"""The cube, tick by tick, as measured in the game (SPEC.md 0.2c, 0.2d).

One sample per tick, numbered like `run_replay`'s trace. Sample 0 is the state before
anything runs; sample 1 follows the first command phase with no move; every later
sample t follows, in this order (S-L3 probes):

1. **move**: x += speed; if the cube is no longer supported it is airborne; if airborne,
   vy -= gravity, then y += vy * scale;
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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..constants import SPEEDS, PhysicsConstants
from ..roles import Role
from .world import Body, Box, World

HALF = 15.0          # outer box half-size (measured: 30 wide)
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
            dx[speed] = measured.value / 240.0
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
        )


@dataclass
class Cube:
    x: float = 0.0
    y: float = HALF
    vy: float = 0.0
    speed: str = "1x"
    grounded: bool = True
    holding: bool = False
    buffered: bool = False
    used: set[int] = field(default_factory=set)
    touching_orbs: list[Body] = field(default_factory=list)
    dead: bool = False
    was_grounded: bool = True

    def box(self, half: float = HALF) -> Box:
        return Box(self.x - half, self.y - half, self.x + half, self.y + half)

    def key(self) -> tuple:
        """What the solver deduplicates on (LEVEL-DESIGN-SPEC.md 6.8.2)."""
        return (round(self.y * 2) / 2, round(self.vy * 20) / 20, self.speed, self.grounded,
                self.holding, self.buffered, frozenset(self.used))

    def copy(self) -> Cube:
        clone = Cube(self.x, self.y, self.vy, self.speed, self.grounded, self.holding,
                     self.buffered, set(self.used), list(self.touching_orbs), self.dead,
                     self.was_grounded)
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
    cube.x += p.dx[cube.speed]
    if cube.grounded and cube.holding:
        # A held button jumps again from inside the move, with no gravity that tick: in
        # P3 the cube that landed at sample 164 is already at 15 + 11.18 x 0.225 at 165.
        cube.vy, cube.grounded, cube.buffered = p.jump_velocity, False, False
        cube.y += cube.vy * p.scale
        return
    if cube.grounded and not _supported(cube, near):
        cube.grounded = False
    if not cube.grounded:
        cube.vy -= p.gravity
        cube.y += cube.vy * p.scale


def collide(cube: Cube, near: list[Body], p: CubeParams, tick: int, prev_y: float,
            prev_x: float | None = None) -> Death | None:
    """Resolve contacts after a move. `near` holds every body the cube can touch this
    tick, before or after the move. Returns the death, if this tick kills."""
    if cube.y - HALF <= FLOOR:
        cube.y, cube.vy, cube.grounded = FLOOR + HALF, 0.0, True
    outer = cube.box()
    if not near:
        cube.touching_orbs = []
        return None
    # Solids first: landing changes y, which every later check depends on.
    for body in near:
        if body.role is not Role.SOLID or not body.box.overlaps(outer):
            continue
        top = body.box.y2
        if cube.vy <= 0 and prev_y - HALF >= top - p.landing_tolerance:
            cube.y, cube.vy, cube.grounded = top + HALF, 0.0, True
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
            cube.used.add(body.uid)
            cube.vy = p.pad_impulse[body.variant or "yellow"]
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
                cube.vy, cube.grounded, cube.buffered = p.jump_velocity, False, False
                return
        elif action == "release":
            cube.holding = cube.buffered = False
    if cube.buffered and cube.touching_orbs:
        orb = cube.touching_orbs[0]
        cube.used.add(orb.uid)
        cube.vy, cube.grounded, cube.buffered = p.orb_impulse[orb.variant or "yellow"], False, False


def advance(cube: Cube, world: World, p: CubeParams, tick: int,
            actions: Iterable[str] = ()) -> Death | None:
    """One tick: move, collide, then the commands for `tick - 1`. Returns a death."""
    prev_x, prev_y = cube.x, cube.y
    # One broad-phase lookup per tick, covering the cube before and after its move.
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
