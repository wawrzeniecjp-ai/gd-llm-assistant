"""The cube, tick by tick, as measured in the game (SPEC.md 0.2c, 0.2d).

One sample per tick, numbered like `run_replay`'s trace. Sample 0 is the state before
anything runs; sample 1 follows the first command phase with no move; every later
sample t follows, in this order (S-L3 probes):

1. **move**: x += speed, rounded to a 32-bit float as in the game; if the cube is no
   longer supported it is airborne; if airborne, vy -= gravity (falling no faster than
   terminal velocity), then y += vy * scale;
2. **collide**: the floor and solid tops land the cube; a solid hit by the inner box
   kills; a hazard touched by the outer box kills; a pad sets vy at once; touching a
   speed portal changes the speed from the next move; orbs touched are remembered;
3. **commands** for tick t-1: scripted presses and releases; a buffered press fires a
   touched orb; a grounded cube that is holding jumps.

So a press at tick N first moves the cube at sample N+2 and its vy shows at N+1, as in
the game. Values the probes could not pin down are parameters with the measured bounds,
fitted by the golden tests (`inner_half`, `landing_tolerance`).
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..constants import SPEEDS, PhysicsConstants
from ..roles import Role
from .world import Body, Box, World

HALF = 15.0          # outer box half-size (measured: 30 wide)

_F32 = struct.Struct("<f")


def f32(value: float) -> float:
    """`value` rounded to a 32-bit float, as the game stores the player's x.

    It matters far from the start: past x 16384 the step is 1/512 of a unit, so a 1x
    move of 1.29825 becomes 1.298828 every tick and the cube is half a unit ahead after
    1000 ticks (playtest 2026-11-09: the game's x matched this to 0.0005).
    """
    return _F32.unpack(_F32.pack(value))[0]
FLOOR = 0.0          # the ground's top (S-L1)


class UncalibratedError(RuntimeError):
    """A constant the cube needs has no measured value."""


@dataclass(frozen=True)
class CubeParams:
    dx: Mapping[str, float]          # units per tick, by speed
    gravity: float
    scale: float
    jump_velocity: float
    orb_impulse: Mapping[str, float]
    pad_impulse: Mapping[str, float]
    inner_half: float = 4.5          # measured bounds (3.72, 5.02]; fitted
    landing_tolerance: float = 10.0  # how far below a top the feet may be and still land
    calibrated: bool = True
    #: The jump differs by speed (playtest 2026-11-09: at 0.5x vy0 10.62, gravity 0.212).
    #: A speed missing here uses `jump_velocity` and `gravity`, measured at 1x.
    jump_by_speed: Mapping[str, float] = field(default_factory=dict)
    #: The fastest fall, as a positive number (playtest 2026-11-09: vy stops at -15.0).
    #: None: no cap.
    terminal_velocity: float | None = None
    gravity_by_speed: Mapping[str, float] = field(default_factory=dict)
    #: Jump snapping (the game's `PlayerObject::checkSnapJumpToObject`, calibration
    #: P10-P12 2026-11-09). Landing on a solid one block up or down from the solid the
    #: cube jumped from, at a spacing between their centres inside a window for that speed,
    #: moves the cube towards where it took off on the old solid, mapped onto the new one,
    #: plus one tick of x, by at most the speed's step. By speed: (dy, lo, hi) windows.
    snap_windows: Mapping[str, tuple[tuple[float, float, float], ...]] = field(
        default_factory=dict)
    snap_max_step: Mapping[str, float] = field(default_factory=dict)

    #: Orb and pad impulses by speed (calibration P13); a speed missing here uses the 1x
    #: `orb_impulse` / `pad_impulse`.
    orb_by_speed: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    pad_by_speed: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    def orb_for(self, speed: str, color: str) -> float:
        return self.orb_by_speed.get(speed, {}).get(color, self.orb_impulse[color])

    def pad_for(self, speed: str, color: str) -> float:
        return self.pad_by_speed.get(speed, {}).get(color, self.pad_impulse[color])

    def jump_for(self, speed: str) -> float:
        return self.jump_by_speed.get(speed, self.jump_velocity)

    def gravity_for(self, speed: str) -> float:
        return self.gravity_by_speed.get(speed, self.gravity)

    @property
    def uncalibrated_speeds(self) -> list[str]:
        """Speeds whose jump is assumed to be the 1x one, not measured."""
        return [s for s in SPEEDS if s != "1x"
                and (s not in self.jump_by_speed or s not in self.gravity_by_speed)]

    @classmethod
    def from_constants(cls, constants: PhysicsConstants) -> CubeParams:
        def need(*path: str) -> float:
            value = constants.mode_value("cube", *path)
            if value is None or value.value is None:
                raise UncalibratedError(f"cube.{'.'.join(path)} is not calibrated")
            return value.value

        dx = {}
        calibrated = True
        for speed in SPEEDS:
            measured = constants.units_per_second[speed]
            if measured.value is None:
                raise UncalibratedError(f"units_per_second.{speed} is not calibrated")
            calibrated &= measured.calibrated
            dx[speed] = f32(measured.value / 240.0)
        jumps, gravities = {}, {}
        orbs: dict[str, dict[str, float]] = {}
        pads: dict[str, dict[str, float]] = {}
        for speed in SPEEDS:
            for kind, into in (("orb_impulse", orbs), ("pad_impulse", pads)):
                for color in ("yellow", "pink"):
                    value = constants.mode_value("cube", "by_speed", speed, kind, color)
                    if value is not None and value.value is not None:
                        into.setdefault(speed, {})[color] = value.value
            if speed == "1x":
                continue
            jump = constants.mode_value("cube", "by_speed", speed, "jump_velocity")
            gravity = constants.mode_value("cube", "by_speed", speed, "gravity")
            if jump is not None and jump.value is not None:
                jumps[speed] = jump.value
            if gravity is not None and gravity.value is not None:
                gravities[speed] = gravity.value
        snap = constants.modes.get("cube", {}).get("jump_snap") or {}
        windows: dict[str, tuple[tuple[float, float, float], ...]] = {}
        max_steps: dict[str, float] = {}
        for speed in SPEEDS:
            table = snap.get(speed) or {}
            found = []
            for kind, dy in (("up", 30.0), ("down", -30.0)):
                bounds = table.get(kind) or {}
                lo, hi = bounds.get("lo"), bounds.get("hi")
                if lo is not None and hi is not None and lo.value is not None \
                        and hi.value is not None:
                    found.append((dy, lo.value, hi.value))
            if found and table.get("max_step") is not None:
                windows[speed] = tuple(found)
                max_steps[speed] = table["max_step"].value
        terminal = constants.mode_value("cube", "terminal_velocity")
        inner = constants.mode_value("cube", "inner_hitbox")
        tolerance = constants.mode_value("cube", "landing_tolerance")
        return cls(
            dx=dx,
            gravity=need("gravity"),
            scale=need("velocity_scale"),
            jump_velocity=need("jump_velocity"),
            orb_impulse={k: need("orb_impulse", k) for k in ("yellow", "pink")},
            pad_impulse={k: need("pad_impulse", k) for k in ("yellow", "pink")},
            inner_half=inner.value / 2 if inner and inner.value else 4.5,
            landing_tolerance=tolerance.value if tolerance and tolerance.value else 10.0,
            calibrated=calibrated and bool(inner and inner.calibrated)
            and bool(tolerance and tolerance.calibrated),
            jump_by_speed=jumps,
            terminal_velocity=terminal.value if terminal and terminal.value else None,
            gravity_by_speed=gravities,
            snap_windows=windows,
            orb_by_speed=orbs,
            pad_by_speed=pads,
            snap_max_step=max_steps,
        )


@dataclass(slots=True)
class Cube:
    x: float = 0.0
    y: float = HALF
    vy: float = 0.0
    speed: str = "1x"
    grounded: bool = True
    holding: bool = False
    buffered: bool = False
    used: frozenset[int] = frozenset()
    touching_orbs: list[Body] = field(default_factory=list)
    dead: bool = False
    was_grounded: bool = True
    #: The centre of the solid the cube last landed on (None: the floor), and its x on
    #: the last tick it was grounded: what jump snapping measures from.
    snap: tuple[float, float] | None = None
    ground_x: float = 0.0

    def box(self, half: float = HALF) -> Box:
        return Box(self.x - half, self.y - half, self.x + half, self.y + half)

    def key(self) -> tuple:
        """What the solver deduplicates on (LEVEL-DESIGN-SPEC.md 6.8.2)."""
        return (round(self.y * 2) / 2, round(self.vy * 20) / 20, self.speed, self.grounded,
                self.holding, self.buffered, self.used, self.snap)

    def copy(self) -> Cube:
        clone = Cube(self.x, self.y, self.vy, self.speed, self.grounded, self.holding,
                     self.buffered, self.used, self.touching_orbs, self.dead,
                     self.was_grounded, self.snap, self.ground_x)
        return clone


@dataclass
class Death:
    tick: int
    x: float
    y: float
    cause_uid: int | None
    cause: str


def _supported(cube: Cube, near: list[Body]) -> bool:
    feet = cube.y - HALF
    if abs(feet - FLOOR) < 1e-6:
        return True
    for body in near:
        if body.role is Role.SOLID and abs(body.box.y2 - feet) < 1e-6 \
                and body.box.x1 < cube.x + HALF and cube.x - HALF < body.box.x2:
            return True
    return False


def move(cube: Cube, near: list[Body], p: CubeParams) -> None:
    cube.was_grounded = cube.grounded
    if cube.grounded:
        cube.ground_x = cube.x
    cube.x = f32(cube.x + p.dx[cube.speed])
    if cube.grounded and cube.holding:
        # A held button jumps again from inside the move, with no gravity that tick: in
        # P3 the cube that landed at sample 164 is already at 15 + 11.18 x 0.225 at 165.
        cube.vy, cube.grounded, cube.buffered = p.jump_for(cube.speed), False, False
        cube.y += cube.vy * p.scale
        return
    if cube.grounded and not _supported(cube, near):
        cube.grounded = False
    if not cube.grounded:
        cube.vy -= p.gravity_for(cube.speed)
        if p.terminal_velocity is not None and cube.vy < -p.terminal_velocity:
            cube.vy = -p.terminal_velocity
        cube.y += cube.vy * p.scale


def _snap_jump(cube: Cube, body: Body, p: CubeParams) -> None:
    """Jump snapping on landing on `body` (see `CubeParams.snap_pairs`)."""
    cx, cy = (body.box.x1 + body.box.x2) / 2, (body.box.y1 + body.box.y2) / 2
    if cube.snap is not None and cube.snap != (cx, cy):
        spacing, rise = cx - cube.snap[0], cy - cube.snap[1]
        for dy, lo, hi in p.snap_windows.get(cube.speed, ()):
            if abs(rise - dy) < 0.5 and lo <= spacing <= hi:
                # The game measures from one tick after the last grounded one (P12: 2x,
                # spacing 150 moved 1.489, exactly that, below the 2-unit cap).
                target = cx + (cube.ground_x + p.dx[cube.speed] - cube.snap[0])
                limit = p.snap_max_step[cube.speed]
                cube.x = f32(cube.x + max(-limit, min(limit, target - cube.x)))
                break
    cube.snap = (cx, cy)
    # A press on the landing tick jumps before the next move records it (P11 A, run 1).
    cube.ground_x = cube.x


def _covered(body: Body, near: list[Body], outer: Box) -> bool:
    """Is the top of `body`, where the cube is over it, under another solid sitting flush
    on it? A stacked column's inner tops are not floors: without this the cube landed on
    a 3-block step's middle block from the side and jumped on from inside it (slot 12)."""
    top = body.box.y2
    x1, x2 = max(body.box.x1, outer.x1), min(body.box.x2, outer.x2)
    for other in near:
        if (other is not body and other.role is Role.SOLID
                and abs(other.box.y1 - top) < 1e-6 and other.box.y2 > top
                and other.box.x1 <= x1 and other.box.x2 >= x2):
            return True
    return False


def collide(cube: Cube, near: list[Body], p: CubeParams, tick: int, prev_y: float,
            prev_x: float | None = None) -> Death | None:
    """Resolve contacts after a move. `near` holds every body the cube can touch this
    tick, before or after the move. Returns the death, if this tick kills."""
    if cube.y - HALF <= FLOOR:
        cube.y, cube.vy, cube.grounded = FLOOR + HALF, 0.0, True
        cube.snap = None
    outer = cube.box()
    if not near:
        cube.touching_orbs = []
        return None
    # Solids first: landing changes y, which every later check depends on.
    for body in near:
        if body.role is not Role.SOLID or not body.box.overlaps(outer):
            continue
        top = body.box.y2
        if _covered(body, near, outer):
            continue
        if cube.vy <= 0 and prev_y - HALF >= top - p.landing_tolerance:
            landed_now = not cube.was_grounded
            cube.y, cube.vy, cube.grounded = top + HALF, 0.0, True
            if landed_now:
                _snap_jump(cube, body, p)
            outer = cube.box()
    inner = cube.box(p.inner_half)
    # Orbs are touched at the position before the move as well as after (P6: a press at
    # 222 fired with the cube's edge 0.2 past the orb after the move, inside it before).
    before = (Box(prev_x - HALF, prev_y - HALF, prev_x + HALF, prev_y + HALF)
              if prev_x is not None else outer)
    cube.touching_orbs = []
    for body in near:
        if (body.role is Role.ORB and body.uid not in cube.used
                and (body.touches(outer) or body.touches(before))):
            cube.touching_orbs.append(body)
    for body in near:
        if body.role is Role.SOLID:
            if body.box.overlaps(inner):
                return Death(tick, cube.x, cube.y, body.uid, "solid")
        elif not body.touches(outer):
            continue
        elif body.role is Role.HAZARD:
            return Death(tick, cube.x, cube.y, body.uid, "hazard")
        elif body.role is Role.PAD and body.uid not in cube.used:
            cube.used = cube.used | {body.uid}
            cube.vy = p.pad_for(cube.speed, body.variant or "yellow")
            cube.grounded = False
        elif body.role is Role.PORTAL_SPEED and body.variant:
            cube.speed = body.variant
    return None


def commands(cube: Cube, p: CubeParams, actions: Iterable[str]) -> None:
    for action in actions:
        if action == "press":
            cube.holding = cube.buffered = True
            if cube.grounded:
                # A fresh press jumps at once: vy is set now, the cube moves next tick
                # (P2: vy 11.18 at sample 61, y still 15).
                cube.vy, cube.grounded, cube.buffered = p.jump_for(cube.speed), False, False
                return
        elif action == "release":
            # A press buffered in the air still fires an orb first touched on the tick
            # it is released (P13, every speed: a one-tick press one tick before the
            # first touch fired, on the release tick).
            _fire_orb(cube, p)
            cube.holding = cube.buffered = False
    _fire_orb(cube, p)


def _fire_orb(cube: Cube, p: CubeParams) -> None:
    if cube.buffered and cube.touching_orbs:
        orb = cube.touching_orbs[0]
        cube.used = cube.used | {orb.uid}
        cube.vy = p.orb_for(cube.speed, orb.variant or "yellow")
        cube.grounded, cube.buffered = False, False


def advance(cube: Cube, world: World, p: CubeParams, tick: int,
            actions: Iterable[str] = (), near: list[Body] | None = None) -> Death | None:
    """One tick: move, collide, then the commands for `tick - 1`. Returns a death.
    `near` is the broad-phase lookup for this cube's x and speed, when the caller has it."""
    prev_x, prev_y = cube.x, cube.y
    # One broad-phase lookup per tick, covering the cube before and after its move.
    if near is None:
        near = world.near(prev_x - HALF, prev_x + p.dx[cube.speed] + HALF)
    move(cube, near, p)
    death = collide(cube, near, p, tick, prev_y, prev_x)
    if death is not None:
        cube.dead = True
        return death
    commands(cube, p, actions)
    return None


@dataclass
class SimResult:
    outcome: str                 # "death" | "max_ticks" | "reached"
    ticks: int
    death: Death | None
    trace: dict[str, list[Any]]
    final: Cube


def simulate(world: World, p: CubeParams, inputs: Iterable[Mapping[str, Any]], *,
             max_ticks: int, until_x: float | None = None,
             start: Cube | None = None, start_tick: int = 0,
             record: bool = True) -> SimResult:
    """Play an input script. Inputs are `{tick, action}` in replay ticks."""
    by_tick: dict[int, list[str]] = {}
    for item in inputs:
        by_tick.setdefault(int(item["tick"]), []).append(str(item["action"]))
    cube = start.copy() if start is not None else Cube()
    trace: dict[str, list[Any]] = {k: [] for k in ("tick", "x", "y", "vy", "grounded")}

    def sample(t: int) -> None:
        if record:
            for key, value in zip(trace, (t, cube.x, cube.y, cube.vy, int(cube.grounded)),
                                  strict=True):
                trace[key].append(value)

    t = start_tick
    if start is None:
        sample(0)
        commands(cube, p, by_tick.get(0, ()))
        t = 1
        sample(1)
    while t < max_ticks:
        t += 1
        death = advance(cube, world, p, t, by_tick.get(t - 1, ()))
        sample(t)
        if death is not None:
            return SimResult("death", t, death, trace, cube)
        if until_x is not None and cube.x >= until_x:
            return SimResult("reached", t, None, trace, cube)
    return SimResult("max_ticks", t, None, trace, cube)
