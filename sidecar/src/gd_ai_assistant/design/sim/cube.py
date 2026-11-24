"""The player, tick by tick, as measured in the game (SPEC.md 0.2c, 0.2d).

The module is named for the cube, Sim-1's only mode; Sim-2 (M5) adds ship, ball, ufo and
wave, gravity portals, mini, and blue orbs and pads, on the same state (`Cube`).

One sample per tick, numbered like `run_replay`'s trace. Sample 0 is the state before
anything runs; sample 1 follows the first command phase with no move; every later
sample t follows, in this order (S-L3 probes):

1. **move**: x += speed, rounded to a 32-bit float as in the game; then the mode's
   vertical step (the cube: if no longer supported it is airborne; if airborne, vy -=
   gravity, falling no faster than terminal velocity; then y += vy * scale);
2. **collide**: the floor and the mode's ceiling; gravity, mode and size portals (before
   solids: P16, a cube flipped while standing on a block leaves it before landing is
   resolved); solid tops land the cube; a solid hit by the inner box kills; a hazard
   touched by the outer box kills; a pad sets vy at once; touching a speed portal
   changes the speed from the next move; orbs touched are remembered;
3. **commands** for tick t-1: scripted presses and releases; a buffered press fires a
   touched orb; a grounded cube that is holding jumps; a grounded ball flips.

So a press at tick N first moves the cube at sample N+2 and its vy shows at N+1, as in
the game. Values the probes could not pin down are parameters with the measured bounds,
fitted by the golden tests (`inner_half`, `landing_tolerance`).

vy is in world terms (positive is up), as the game's trace shows it in either gravity;
`vy * gravity` is vy relative to gravity.

What Sim-2 does not know yet is reported, not guessed: a mini cube's orbs, a grounded
mini cube under a block deeper than `mini_cube_squeeze`, a ship, ufo or ball meeting a
block from below between `ceiling_push` and `ceiling_kill` deep, and Sim-3's modes end
the run as `unverifiable` with the reason.
"""

from __future__ import annotations

import math
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
#: Modes the simulator plays (Sim-2, and the robot of Sim-3).
MODES = ("cube", "ship", "ball", "ufo", "wave", "robot")
#: Modes that run on the ground and jump: the cube's moves and contacts, each with its
#: own jump. The others fly within a band.
GROUND_MODES = frozenset({"cube", "robot"})
#: How close (units) a flight-mode or mini player may come to an object whose contact is
#: not calibrated (see `_uncalibrated_contact`) before the run is unverifiable.
CONTACT_MARGIN = 3.0
#: A mode portal's floor is its centre less half the band, rounded down to this grid, and
#: never below the ground (P17: portals at y 143, 192, 238 and 284 gave floors 0, 30, 60
#: and 120 for the 300-high ufo and wave bands, and 0, 60, 90 and 150 for the ball's 240).
BAND_GRID = 30.0


#: Why an upside-down robot stops a run: P19's flipped runs had no roof, so its jump
#: upside down is not measured yet.
UPSIDE_DOWN_ROBOT = "an upside-down robot is not calibrated yet"


class UncalibratedError(RuntimeError):
    """A constant the cube needs has no measured value."""


@dataclass(frozen=True)
class FlightParams:
    """Sim-2: ship, ball, ufo and wave, gravity, mini and blue (calibration P15/P16,
    2026-11-15). Accelerations are per tick, as measured (each over hundreds of ticks:
    they are not exact multiples of one gravity); `vr = vy * gravity` is vy relative to
    gravity."""
    #: Ship and ufo count as falling while `gravity * (vy - falling_below) < 0`:
    #: below +1.916 world vy upright, above it upside down (P15, both gravities).
    falling_below: float
    #: Mini half-size factor (0.6: a mini cube rests at y 9).
    mini_scale: float
    #: Mini multiplies the cube's jump, pads and the ball's flip by this (0.8).
    mini_impulse: float
    #: Blue orbs and pads: this times the yellow one, towards the new floor (0.4).
    blue: float
    #: A gravity portal multiplies vy by this (0.5).
    gravity_portal_vy: float
    #: A ship, ufo or ball meeting a solid from below is pushed down when the solid is at
    #: most `ceiling_push` into its outer box and dies from `ceiling_kill` (both times
    #: mini_scale when mini; P17/P18); in between is not known.
    ceiling_push: float
    ceiling_kill: float
    #: A grounded mini cube runs on under a solid at most this far into its outer box
    #: (P18); deeper, the game pushes it below the ground, which is not simulated.
    mini_cube_squeeze: float
    ship: Mapping[str, float]
    ufo: Mapping[str, float]
    #: The mini ship's and ufo's own values (close to, not exactly, normal / 0.85).
    ship_mini: Mapping[str, float]
    ufo_mini: Mapping[str, float]
    ball: Mapping[str, float]
    wave: Mapping[str, float]
    #: Height of each mode's band between floor and ceiling.
    bounds: Mapping[str, float]
    #: vy factor of each mode at a mode portal, applied for the mode left and again for
    #: the mode entered (P15/P17: cube to ship x0.5, ship to ufo x0.25, ship to ball
    #: x0.5, ball to cube x1); a mode missing here is not calibrated.
    entry: Mapping[str, float]
    #: Yellow and pink orbs and pads by flight mode (P18, the same at 1x and 2x); the wave
    #: has none: they leave its path as it is. A mode missing here is not calibrated.
    orb: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    pad: Mapping[str, Mapping[str, float]] = field(default_factory=dict)

    @classmethod
    def from_constants(cls, constants: PhysicsConstants) -> FlightParams | None:
        """None when Sim-2 is not calibrated: mode portals are then unverifiable."""
        def table(mode: str, *keys: str, sub: tuple[str, ...] = ()) -> dict[str, float] | None:
            out = {}
            for key in keys:
                value = constants.mode_value(mode, *sub, key)
                if value is None or value.value is None:
                    return None
                out[key] = value.value
            return out

        player = table("player", "falling_below", "mini_scale", "mini_impulse", "blue",
                       "gravity_portal_vy", "ceiling_push", "ceiling_kill",
                       "mini_cube_squeeze")
        ship = table("ship", "hold_rising", "hold_falling", "release_rising",
                     "release_falling", "max_up", "max_down", "bounds", "entry_vy")
        ufo = table("ufo", "jump", "rising", "falling", "max_up", "max_down", "bounds",
                    "entry_vy")
        ship_mini = table("ship", "hold_rising", "hold_falling", "release_rising",
                          "release_falling", "max_up", "max_down", sub=("mini",))
        ufo_mini = table("ufo", "jump", "rising", "falling", "max_up", "max_down",
                         sub=("mini",))
        ball = table("ball", "gravity", "flip", "max_speed", "bounds", "entry_vy")
        wave = table("wave", "slope", "y_scale", "mini_y_scale", "half", "contact_half",
                     "solid_half", "bounds", "entry_vy")
        if (player is None or ship is None or ufo is None or ball is None or wave is None
                or ship_mini is None or ufo_mini is None):
            return None
        modes = {"ship": ship, "ufo": ufo, "ball": ball, "wave": wave}
        entry = {name: t["entry_vy"] for name, t in modes.items()}
        for ground in ("cube", "robot"):
            ground_entry = constants.mode_value(ground, "entry_vy")
            if ground_entry is not None and ground_entry.value is not None:
                entry[ground] = ground_entry.value
        orb, pad = {}, {}
        for name in ("ship", "ufo", "ball"):
            orbs = table(name, "yellow", "pink", sub=("orb_impulse",))
            pads = table(name, "yellow", "pink", sub=("pad_impulse",))
            if orbs is not None and pads is not None:
                orb[name], pad[name] = orbs, pads
        return cls(falling_below=player["falling_below"], mini_scale=player["mini_scale"],
                   mini_impulse=player["mini_impulse"], blue=player["blue"],
                   gravity_portal_vy=player["gravity_portal_vy"],
                   ceiling_push=player["ceiling_push"], ceiling_kill=player["ceiling_kill"],
                   mini_cube_squeeze=player["mini_cube_squeeze"], ship=ship, ufo=ufo,
                   ball=ball, wave=wave, ship_mini=ship_mini, ufo_mini=ufo_mini,
                   bounds={name: t["bounds"] for name, t in modes.items()}, entry=entry,
                   orb=orb, pad=pad)

    def impulses_known(self, mode: str) -> bool:
        """Are this flight mode's orbs and pads calibrated? (The wave's need nothing.)"""
        return mode == "wave" or (mode in self.orb and mode in self.pad)


@dataclass(frozen=True)
class RobotParams:
    """The robot (calibration P19, 2026-11-24). A ground mode: it runs, lands and dies as
    the cube does, and only its jump differs. A press on the ground sets vy to
    `boost_ratio` times the cube's jump at that speed (mini: times `mini_impulse`), and vy
    stays there while the press is held, for `boost_ticks` samples at most, counting the
    one it is set on; then the robot falls with its own gravity. Held through a landing
    it does not jump again; a press made in the air and still held at landing does."""

    boost_ratio: float
    boost_ticks: int
    gravity: float
    gravity_by_speed: Mapping[str, float]
    #: Yellow and pink orbs at 1x (10.06 and 8.05: 0.9 and 1.0 of the cube's); other
    #: speeds scale the cube's by the same ratio.
    orb: Mapping[str, float]

    def gravity_for(self, speed: str) -> float:
        return self.gravity_by_speed.get(speed, self.gravity)

    @classmethod
    def from_constants(cls, constants: PhysicsConstants) -> RobotParams | None:
        def get(*path: str) -> float | None:
            value = constants.mode_value("robot", *path)
            return None if value is None or value.value is None else value.value

        ratio, ticks, gravity = get("boost_ratio"), get("boost_ticks"), get("gravity")
        yellow, pink = get("orb_impulse", "yellow"), get("orb_impulse", "pink")
        if None in (ratio, ticks, gravity, yellow, pink):
            return None
        by_speed = {}
        for speed in SPEEDS:
            value = get("by_speed", speed, "gravity")
            if value is not None:
                by_speed[speed] = value
        assert ratio is not None and ticks is not None and gravity is not None
        assert yellow is not None and pink is not None
        return cls(boost_ratio=ratio, boost_ticks=int(ticks), gravity=gravity,
                   gravity_by_speed=by_speed, orb={"yellow": yellow, "pink": pink})


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
    #: Sim-2 (None: not calibrated, so mode, gravity and size portals are unverifiable).
    flight: FlightParams | None = None
    #: Sim-3's robot (None: not calibrated, so a robot portal is unverifiable).
    robot: RobotParams | None = None
    #: Not physics: a margin the ship and the wave keep from what kills them (hazards; the
    #: wave's solids too), used by the solver to find a forgiving route. It only adds
    #: deaths, so a route that survives with it survives without. 0 in the game's physics.
    clearance: float = 0.0

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
            flight=FlightParams.from_constants(constants),
            robot=RobotParams.from_constants(constants),
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
    mode: str = "cube"
    gravity: int = 1                 # +1 upright, -1 upside down
    mini: bool = False
    #: The band a flight mode lives in: (floor, ceiling) in level y; None for the cube.
    bounds: tuple[float, float] | None = None
    #: Set when the run meets something Sim-2 does not simulate; ends the run.
    unverifiable: str | None = None
    #: The robot: moves left that keep its boost while the press is held.
    boost: int = 0

    def box(self, half: float = HALF) -> Box:
        return Box(self.x - half, self.y - half, self.x + half, self.y + half)

    def key(self) -> tuple:
        """What the solver deduplicates on (LEVEL-DESIGN-SPEC.md 6.8.2)."""
        return (round(self.y * 2) / 2, round(self.vy * 20) / 20, self.speed, self.grounded,
                self.holding, self.buffered, self.used, self.snap, self.mode, self.gravity,
                self.mini, self.bounds, self.boost)

    def copy(self) -> Cube:
        return Cube(self.x, self.y, self.vy, self.speed, self.grounded, self.holding,
                    self.buffered, self.used, self.touching_orbs, self.dead,
                    self.was_grounded, self.snap, self.ground_x, self.mode, self.gravity,
                    self.mini, self.bounds, self.unverifiable, self.boost)


@dataclass
class Death:
    tick: int
    x: float
    y: float
    cause_uid: int | None
    cause: str                       # "solid" | "hazard" | "unverifiable"
    reason: str | None = None        # for "unverifiable": what is not simulated


def half_of(cube: Cube, p: CubeParams) -> float:
    """The player's outer half-size: 15, the wave 10 (P15: it rests at y 10), times 0.6
    when mini."""
    base = HALF
    fp = p.flight
    if cube.mode == "wave" and fp is not None:
        base = fp.wave["half"]
    if cube.mini and fp is not None:
        base *= fp.mini_scale
    return base


def contact_half(cube: Cube, p: CubeParams) -> float:
    """The half-size the player touches objects with. The wave touches them with its
    small box (P15: upside down, the wave flipped at a gravity portal 3 ticks after the
    others, its edge 3.76-5.06 from the portal; P17: it touched a portal 4.88 from its
    centre, and a spike at 5.00: 5); every other mode with its outer one."""
    if cube.mode == "wave" and p.flight is not None:
        return p.flight.wave["contact_half"] * (p.flight.mini_scale if cube.mini else 1.0)
    return half_of(cube, p)


def _supported(cube: Cube, near: list[Body], half: float) -> bool:
    if cube.gravity > 0:
        feet = cube.y - half
        if abs(feet - FLOOR) < 1e-6:
            return True
        for body in near:
            if body.role is Role.SOLID and abs(body.box.y2 - feet) < 1e-6 \
                    and body.box.x1 < cube.x + half and cube.x - half < body.box.x2:
                return True
        return False
    head = cube.y + half
    for body in near:
        if body.role is Role.SOLID and abs(body.box.y1 - head) < 1e-6 \
                and body.box.x1 < cube.x + half and cube.x - half < body.box.x2:
            return True
    return False


def _impulse_scale(cube: Cube, p: CubeParams) -> float:
    return p.flight.mini_impulse if cube.mini and p.flight is not None else 1.0


def move(cube: Cube, near: list[Body], p: CubeParams) -> None:
    cube.was_grounded = cube.grounded
    if cube.grounded:
        cube.ground_x = cube.x
    cube.x = f32(cube.x + p.dx[cube.speed])
    if cube.mode not in GROUND_MODES:
        _move_flight(cube, p)
        return
    if cube.mode == "robot":
        _move_robot(cube, near, p)
        return
    g = cube.gravity
    if cube.grounded and cube.holding:
        # A held button jumps again from inside the move, with no gravity that tick: in
        # P3 the cube that landed at sample 164 is already at 15 + 11.18 x 0.225 at 165.
        cube.vy = g * p.jump_for(cube.speed) * _impulse_scale(cube, p)
        cube.grounded, cube.buffered = False, False
        cube.y += cube.vy * p.scale
        return
    if cube.grounded and not _supported(cube, near, half_of(cube, p)):
        cube.grounded = False
    if not cube.grounded:
        cube.vy -= g * p.gravity_for(cube.speed)
        if p.terminal_velocity is not None and g * cube.vy < -p.terminal_velocity:
            cube.vy = -g * p.terminal_velocity
        cube.y += cube.vy * p.scale


def _robot_boost(cube: Cube, p: CubeParams) -> None:
    """Start the robot's jump: vy at its boost, kept for the moves that follow."""
    assert p.robot is not None
    cube.vy = (cube.gravity * p.robot.boost_ratio * p.jump_for(cube.speed)
               * _impulse_scale(cube, p))
    cube.boost = p.robot.boost_ticks - 1
    cube.grounded, cube.buffered = False, False


def _move_robot(cube: Cube, near: list[Body], p: CubeParams) -> None:
    """The robot's move (P19). Its boost keeps vy, with no gravity, while held."""
    r = p.robot
    assert r is not None
    g = cube.gravity
    if cube.grounded and cube.holding and cube.buffered:
        # A press made in the air and held to the landing jumps on the next move, moving
        # that tick (P19 C: landed at 414, at 15 + 5.59 x 0.225 at 415).
        _robot_boost(cube, p)
        cube.y += cube.vy * p.scale
        return
    if cube.grounded and not _supported(cube, near, half_of(cube, p)):
        cube.grounded = False
    if cube.grounded:
        return
    if cube.holding and cube.boost > 0:
        cube.boost -= 1
    else:
        cube.boost = 0
        cube.vy -= g * r.gravity_for(cube.speed)
        if p.terminal_velocity is not None and g * cube.vy < -p.terminal_velocity:
            cube.vy = -g * p.terminal_velocity
    cube.y += cube.vy * p.scale


def _move_flight(cube: Cube, p: CubeParams) -> None:
    """Ship, ufo, ball and wave (P15). Each reads `holding` (and the ufo `buffered`) as the
    commands of the tick before left them."""
    fp = p.flight
    assert fp is not None
    g = cube.gravity
    falling = g * (cube.vy - fp.falling_below) < 0
    vr = cube.vy * g
    if cube.mode == "ship":
        s = fp.ship_mini if cube.mini else fp.ship
        if cube.holding:
            vr += s["hold_falling"] if falling else s["hold_rising"]
        else:
            vr -= s["release_falling"] if falling else s["release_rising"]
        vr = max(-s["max_down"], min(s["max_up"], vr))
    elif cube.mode == "ufo":
        u = fp.ufo_mini if cube.mini else fp.ufo
        if cube.buffered:
            # A press jumps on the next move, replacing vy, with no gravity that tick
            # (P15: pressed at 320, vy 6.871 and y + 6.871 x 0.225 at 322). Held, it
            # jumps once.
            vr = u["jump"]
            cube.buffered = False
        else:
            vr -= u["falling"] if falling else u["rising"]
            vr = max(-u["max_down"], min(u["max_up"], vr))
    elif cube.mode == "ball":
        b = fp.ball
        vr -= b["gravity"]
        vr = max(-b["max_speed"], min(b["max_speed"], vr))
    else:  # wave: a fixed slope, 4 x the speed's dx in vy, y moving a quarter of it
        w = fp.wave
        vr = (1.0 if cube.holding else -1.0) * w["slope"] * p.dx[cube.speed]
        cube.vy = vr * g
        cube.y += cube.vy * (w["mini_y_scale"] if cube.mini else w["y_scale"])
        cube.grounded = False
        return
    cube.vy = vr * g
    cube.y += cube.vy * p.scale
    cube.grounded = False


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


def _covered(body: Body, near: list[Body], outer: Box, gravity: int = 1) -> bool:
    """Is the face of `body` the cube would land on, where the cube is over it, under
    another solid sitting flush on it? A stacked column's inner tops are not floors:
    without this the cube landed on a 3-block step's middle block from the side and
    jumped on from inside it (slot 12). Upside down, the same for bottoms."""
    face = body.box.y2 if gravity > 0 else body.box.y1
    x1, x2 = max(body.box.x1, outer.x1), min(body.box.x2, outer.x2)
    for other in near:
        if other is body or other.role is not Role.SOLID:
            continue
        if gravity > 0:
            flush = abs(other.box.y1 - face) < 1e-6 and other.box.y2 > face
        else:
            flush = abs(other.box.y2 - face) < 1e-6 and other.box.y1 < face
        if flush and other.box.x1 <= x1 and other.box.x2 >= x2:
            return True
    return False


_CONTACT_ROLES = frozenset({Role.SOLID, Role.HAZARD, Role.ORB, Role.PAD})


def _uncalibrated_contact(cube: Cube, near: list[Body], half: float,
                          fp: FlightParams | None) -> Body | None:
    """An orb or pad near a flight mode whose orbs and pads are not calibrated. Every
    other contact of the Sim-2 modes is: the cube's (Sim-1), solids and hazards in every
    mode and size (P17, P18), pads (P16, P18) and flight orbs (P18). A mini cube's orbs
    are checked when one fires."""
    if cube.mode in GROUND_MODES or (fp is not None and fp.impulses_known(cube.mode)):
        return None
    reach = Box(cube.x - half - CONTACT_MARGIN, cube.y - half - CONTACT_MARGIN,
                cube.x + half + CONTACT_MARGIN, cube.y + half + CONTACT_MARGIN)
    for body in near:
        if body.role in (Role.ORB, Role.PAD) and body.touches(reach):
            return body
    return None


def _bounds_for(mode: str, portal: Body, fp: FlightParams) -> tuple[float, float]:
    """The band a mode portal sets: the portal's centre less half the band, rounded down
    to the 30 grid and no lower than the ground (see `BAND_GRID`)."""
    height = fp.bounds[mode]
    centre = (portal.box.y1 + portal.box.y2) / 2
    floor = max(FLOOR, math.floor((centre - height / 2) / BAND_GRID + 1e-9) * BAND_GRID)
    return (floor, floor + height)


def _portal(cube: Cube, body: Body, p: CubeParams, half: float) -> None:
    """Gravity, mode and size portals, each once."""
    cube.used = cube.used | {body.uid}
    fp = p.flight
    if fp is None:
        cube.unverifiable = f"{body.role.value} portals are not calibrated"
        return
    if body.role is Role.PORTAL_GRAVITY:
        new = {"normal": 1, "inverse": -1}.get(body.variant or "", -cube.gravity)
        if new == cube.gravity:
            return
        if cube.mode == "robot":
            cube.unverifiable = UPSIDE_DOWN_ROBOT
            return
        if cube.mode == "cube" and cube.grounded and not (
                cube.gravity > 0 and abs(cube.y - half - FLOOR) < 1e-6):
            # Standing on a block, the game's move already pulled the cube one tick of
            # gravity into it, and the flip comes before landing undoes that (P16:
            # flipped on the roof, y 195.049 and vy 0.108 on the flip tick).
            cube.vy = -cube.gravity * p.gravity_for(cube.speed)
            cube.y += cube.vy * p.scale
        cube.vy *= fp.gravity_portal_vy
        cube.gravity = new
        cube.grounded, cube.snap = False, None
    elif body.role is Role.PORTAL_SIZE:
        cube.mini = body.variant == "mini"
    elif body.role is Role.PORTAL_MODE:
        target = body.variant or ""
        if target == cube.mode:
            return
        if target not in MODES:
            cube.unverifiable = f"the {target} is Sim-3, not simulated yet"
            return
        if target not in fp.entry or cube.mode not in fp.entry or (
                target == "robot" and p.robot is None):
            cube.unverifiable = f"entering the {target} from the {cube.mode} is not calibrated"
            return
        if target == "robot" and cube.gravity < 0:
            cube.unverifiable = UPSIDE_DOWN_ROBOT
            return
        bounds = _bounds_for(target, body, fp) if target not in GROUND_MODES else None
        cube.vy *= fp.entry[cube.mode] * fp.entry[target]
        cube.mode, cube.bounds = target, bounds
        cube.grounded, cube.snap, cube.buffered, cube.boost = False, None, False, 0


def collide(cube: Cube, near: list[Body], p: CubeParams, tick: int, prev_y: float,
            prev_x: float | None = None) -> Death | None:
    """Resolve contacts after a move. `near` holds every body the cube can touch this
    tick, before or after the move. Returns the death, if this tick kills."""
    half = half_of(cube, p)
    g = cube.gravity
    _bands(cube, half)
    outer = cube.box(contact_half(cube, p))
    if not near:
        cube.touching_orbs = []
        return None
    # Gravity, mode and size portals before solids (P16).
    for body in near:
        if (body.role in (Role.PORTAL_GRAVITY, Role.PORTAL_MODE, Role.PORTAL_SIZE)
                and body.uid not in cube.used and body.touches(outer)):
            _portal(cube, body, p, half)
            if cube.unverifiable:
                return None
            half, g = half_of(cube, p), cube.gravity
            outer = cube.box(contact_half(cube, p))
    contact = _uncalibrated_contact(cube, near, contact_half(cube, p), p.flight)
    if contact is not None:
        cube.unverifiable = (f"the {cube.mode} meeting a {contact.role.value} is not "
                             f"calibrated yet (object {contact.uid})")
        return None
    if cube.mode not in GROUND_MODES:
        return _collide_flight(cube, near, p, tick, prev_y, half, prev_x)
    # Solids first: landing changes y, which every later check depends on.
    for body in near:
        if body.role is not Role.SOLID or not body.box.overlaps(outer):
            continue
        if _covered(body, near, outer, g):
            continue
        if g > 0:
            top = body.box.y2
            lands = cube.vy <= 0 and prev_y - half >= top - p.landing_tolerance
            rest = top + half
        else:
            bottom = body.box.y1
            lands = cube.vy >= 0 and prev_y + half <= bottom + p.landing_tolerance
            rest = bottom - half
        if lands:
            landed_now = not cube.was_grounded
            cube.y, cube.vy, cube.grounded = rest, 0.0, True
            if landed_now and g > 0 and not cube.mini:
                _snap_jump(cube, body, p)
            elif landed_now:
                cube.ground_x = cube.x
            outer = cube.box(half)
    if cube.mini and cube.was_grounded and p.flight is not None:
        squeeze = _squeeze(cube, near, outer, prev_y, half)
        if squeeze is not None and squeeze > p.flight.mini_cube_squeeze:
            cube.unverifiable = ("a grounded mini cube under a block this low is not "
                                 "simulated (the game pushes it below the ground)")
            return None
    inner = cube.box(_inner_half(cube, p))
    # Orbs are touched at the position before the move as well as after (P6: a press at
    # 222 fired with the cube's edge 0.2 past the orb after the move, inside it before).
    before = (Box(prev_x - half, prev_y - half, prev_x + half, prev_y + half)
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
        elif body.role is Role.HAZARD:
            if body.hits(outer):
                return Death(tick, cube.x, cube.y, body.uid, "hazard")
        elif not body.touches(outer):
            continue
        elif body.role is Role.PAD and body.uid not in cube.used:
            cube.used = cube.used | {body.uid}
            _launch(cube, p, body.variant or "yellow", p.pad_for)
        elif body.role is Role.PORTAL_SPEED and body.variant:
            cube.speed = body.variant
    return None


def _collide_flight(cube: Cube, near: list[Body], p: CubeParams, tick: int, prev_y: float,
                    half: float, prev_x: float | None = None) -> Death | None:
    """Ship, ufo, ball and wave against solids, hazards, orbs, pads and speed portals.

    - A solid in the inner box kills, checked before any push (P18: a mini ship under a
      block 6 into its outer box died, at 4 it was pushed). The inner box is the cube's,
      at every size.
    - Otherwise a solid the outer box overlaps is a floor if the player came from above
      it, a ceiling if from below (within the cube's landing tolerance: P17, gliding on
      the ground under a block whose bottom was 2-6 into it, the ship, ufo and ball were
      pushed down below the ground, to the block's bottom).
    - The wave never lands or slides: a solid in its small `solid_half` box kills (P18:
      walls, a step's top and side, a ceiling's bottom and side).
    - Pads launch on touch; orbs touched are kept for a press (see `_fire_flight_orb`).
      The wave ignores yellow and pink ones; blue ones flip its gravity (P18)."""
    fp = p.flight
    assert fp is not None
    outer = cube.box(half)
    clear = p.clearance if cube.mode in ("ship", "wave") else 0.0
    if cube.mode == "wave":
        body_box = cube.box(fp.wave["solid_half"] + clear)
        for body in near:
            if body.role is Role.SOLID and body.box.overlaps(body_box):
                return Death(tick, cube.x, cube.y, body.uid, "solid")
    else:
        scale = fp.mini_scale if cube.mini else 1.0
        for body in near:
            if body.role is not Role.SOLID or not body.box.overlaps(outer):
                continue
            if (cube.vy <= 0 and prev_y - half >= body.box.y2 - p.landing_tolerance
                    and not _covered(body, near, outer, 1)):
                cube.y, cube.vy, cube.grounded = body.box.y2 + half, 0.0, cube.gravity > 0
            elif (cube.vy >= 0 and prev_y + half <= body.box.y1 + p.landing_tolerance
                    and not _covered(body, near, outer, -1)):
                depth = cube.y + half - body.box.y1
                if depth >= fp.ceiling_kill * scale:
                    # Reported where the player was before the move (P18: x 410.25 and
                    # y 9, the last live sample, though the box reached the block after).
                    return Death(tick, cube.x if prev_x is None else prev_x, prev_y,
                                 body.uid, "solid")
                if depth > fp.ceiling_push * scale:
                    cube.unverifiable = (f"the {cube.mode} meeting a block {depth:.1f} into "
                                         "it from below is not calibrated")
                    return None
                cube.y, cube.vy, cube.grounded = body.box.y1 - half, 0.0, cube.gravity < 0
            outer = cube.box(half)
    c_half = contact_half(cube, p)
    touch = cube.box(c_half)
    inner = cube.box(_inner_half(cube, p))
    danger = cube.box(c_half + clear) if clear else touch
    # Orbs are touched before the move as well as after, as the cube's (P6).
    before = (Box(prev_x - c_half, prev_y - c_half, prev_x + c_half, prev_y + c_half)
              if prev_x is not None else touch)
    cube.touching_orbs = []
    for body in near:
        if body.role is Role.SOLID:
            if cube.mode != "wave" and body.box.overlaps(inner):
                return Death(tick, cube.x, cube.y, body.uid, "solid")
        elif body.role is Role.HAZARD:
            if body.hits(danger):
                return Death(tick, cube.x, cube.y, body.uid, "hazard")
        elif body.role is Role.ORB:
            if body.uid not in cube.used and (body.touches(touch) or body.touches(before)):
                cube.touching_orbs.append(body)
        elif body.role is Role.PAD:
            # An edge exactly on the player's counts (P18: the gliding wave's box ends
            # at y 5, a floor pad's top).
            if body.uid not in cube.used and body.hits(touch):
                cube.used = cube.used | {body.uid}
                _flight_launch(cube, p, body.variant or "yellow", fp.pad)
        elif body.role is Role.PORTAL_SPEED and body.variant and body.touches(touch):
            cube.speed = body.variant
    return None


def _inner_half(cube: Cube, p: CubeParams) -> float:
    return p.inner_half * (p.flight.mini_scale if cube.mini and p.flight else 1.0)


def _squeeze(cube: Cube, near: list[Body], outer: Box, prev_y: float,
             half: float) -> float | None:
    """How far a solid above the player (below it, upside down) reaches into its outer
    box, or None if none does."""
    deepest = None
    for body in near:
        if body.role is not Role.SOLID or not body.box.overlaps(outer):
            continue
        if cube.gravity > 0 and body.box.y1 > cube.y:
            depth = cube.y + half - body.box.y1
        elif cube.gravity < 0 and body.box.y2 < cube.y:
            depth = body.box.y2 - (cube.y - half)
        else:
            continue
        deepest = depth if deepest is None else max(deepest, depth)
    return deepest


def _flight_launch(cube: Cube, p: CubeParams, color: str,
                   table: Mapping[str, Mapping[str, float]]) -> None:
    """A flight mode's orb or pad (P18): the mode's own yellow and pink values, blue at
    the player's blue factor of the mode's yellow, mini times `mini_impulse`, as the
    cube's. The wave only flips on blue: its path takes no vy."""
    if cube.mode == "wave":
        if color == "blue":
            cube.gravity = -cube.gravity
        return
    values = table[cube.mode]
    _launch(cube, p, color, lambda _speed, c: values[c])


def _fire_flight_orb(cube: Cube, p: CubeParams) -> bool:
    """A press while a flight mode touches an orb fires it instead of the mode's own
    press (P18: no ufo jump, no ball flip); a press held into an orb does not (no
    buffering, unlike the cube). The wave's yellow and pink orbs change nothing."""
    if not cube.touching_orbs or p.flight is None:
        return False
    orb = cube.touching_orbs[0]
    color = orb.variant or "yellow"
    if cube.mode == "wave" and color != "blue":
        return False
    cube.used = cube.used | {orb.uid}
    cube.touching_orbs = []
    _flight_launch(cube, p, color, p.flight.orb)
    return True


def _bands(cube: Cube, half: float) -> None:
    """The ground, and a flight mode's floor and ceiling."""
    floor = cube.bounds[0] if cube.bounds is not None else FLOOR
    if cube.y - half <= floor:
        cube.y, cube.vy = floor + half, 0.0
        cube.grounded = cube.gravity > 0
        cube.snap = None
    if cube.bounds is not None and cube.y + half >= cube.bounds[1]:
        cube.y, cube.vy = cube.bounds[1] - half, 0.0
        cube.grounded = cube.gravity < 0


def _launch(cube: Cube, p: CubeParams, color: str, impulse) -> None:
    """A pad or an orb: yellow and pink push against gravity; blue flips gravity and
    sends the cube towards its new floor at 0.4 of the yellow one (P16)."""
    scale = _impulse_scale(cube, p)
    cube.boost = 0
    if color == "blue" and cube.mode == "robot":
        cube.unverifiable = UPSIDE_DOWN_ROBOT
        return
    if color == "blue":
        assert p.flight is not None
        cube.gravity = -cube.gravity
        cube.vy = -cube.gravity * p.flight.blue * impulse(cube.speed, "yellow") * scale
        cube.snap = None
    else:
        cube.vy = cube.gravity * impulse(cube.speed, color) * scale
    cube.grounded = False


def commands(cube: Cube, p: CubeParams, actions: Iterable[str]) -> None:
    for action in actions:
        if action == "press":
            cube.holding = cube.buffered = True
            if cube.mode not in GROUND_MODES and _fire_flight_orb(cube, p):
                cube.buffered = False
                return
            if cube.mode == "ball":
                if cube.grounded and p.flight is not None:
                    # A grounded press flips the ball at once, at 0.3 of the cube's jump
                    # at that speed, towards the new floor (P15); in the air it does
                    # nothing and is not kept.
                    cube.gravity = -cube.gravity
                    cube.vy = (-cube.gravity * p.flight.ball["flip"]
                               * p.jump_for(cube.speed) * _impulse_scale(cube, p))
                    cube.grounded = cube.buffered = False
                    return
            elif cube.mode == "robot" and cube.grounded and p.robot is not None:
                if cube.touching_orbs:
                    # An orb touched on the ground fires instead of the boost (P19 E:
                    # pressed on the ground inside a yellow orb, vy 10.062, not 5.59).
                    _fire_orb(cube, p)
                    return
                # A fresh press boosts at once: vy is set now, and the robot moves next
                # tick (P19 A: vy 5.59 at 321 for a press at 320, y still 15).
                _robot_boost(cube, p)
                return
            elif cube.mode == "cube" and cube.grounded:
                # A fresh press jumps at once: vy is set now, the cube moves next tick
                # (P2: vy 11.18 at sample 61, y still 15).
                cube.vy = cube.gravity * p.jump_for(cube.speed) * _impulse_scale(cube, p)
                cube.grounded, cube.buffered = False, False
                return
        elif action == "release":
            # A press buffered in the air still fires an orb first touched on the tick
            # it is released (P13, every speed: a one-tick press one tick before the
            # first touch fired, on the release tick).
            _fire_orb(cube, p)
            cube.holding = False
            if cube.mode != "ufo":
                cube.buffered = False
    _fire_orb(cube, p)


def _fire_orb(cube: Cube, p: CubeParams) -> None:
    if cube.buffered and cube.touching_orbs and cube.mode in GROUND_MODES:
        orb = cube.touching_orbs[0]
        color = orb.variant or "yellow"
        if cube.mini:
            cube.unverifiable = f"the mini {cube.mode}'s orbs are not calibrated yet"
            return
        if cube.mode == "robot":
            r = p.robot
            assert r is not None
            cube.used = cube.used | {orb.uid}
            if color == "blue":
                cube.unverifiable = UPSIDE_DOWN_ROBOT
                return
            # P19 E, at 1x; other speeds scale the cube's orb by the same ratio.
            ratio = r.orb[color] / p.orb_for("1x", color)
            _launch(cube, p, color, lambda speed, c: p.orb_for(speed, c) * ratio)
            cube.buffered = False
            return
        if color == "blue" and p.flight is None:
            cube.unverifiable = "blue orbs are not calibrated"
            return
        cube.used = cube.used | {orb.uid}
        _launch(cube, p, color, p.orb_for)
        cube.buffered = False


def advance(cube: Cube, world: World, p: CubeParams, tick: int,
            actions: Iterable[str] = (), near: list[Body] | None = None) -> Death | None:
    """One tick: move, collide, then the commands for `tick - 1`. Returns a death, or
    one with cause "unverifiable" when the run meets something Sim-2 does not know.
    `near` is the broad-phase lookup for this cube's x and speed, when the caller has it."""
    prev_x, prev_y = cube.x, cube.y
    # One broad-phase lookup per tick, covering the cube before and after its move.
    if near is None:
        near = world.near(prev_x - HALF - p.clearance,
                          prev_x + p.dx[cube.speed] + HALF + p.clearance)
    move(cube, near, p)
    death = collide(cube, near, p, tick, prev_y, prev_x)
    if death is None and cube.unverifiable is None:
        commands(cube, p, actions)
    if cube.unverifiable is not None:
        cube.dead = True
        return Death(tick, cube.x, cube.y, None, "unverifiable", cube.unverifiable)
    if death is not None:
        cube.dead = True
        return death
    return None


@dataclass
class SimResult:
    outcome: str                 # "death" | "max_ticks" | "reached" | "unverifiable"
    ticks: int
    death: Death | None
    trace: dict[str, list[Any]]
    final: Cube


TRACE_KEYS = ("tick", "x", "y", "vy", "grounded", "mode", "gravity", "mini")


def simulate(world: World, p: CubeParams, inputs: Iterable[Mapping[str, Any]], *,
             max_ticks: int, until_x: float | None = None,
             start: Cube | None = None, start_tick: int = 0,
             record: bool = True) -> SimResult:
    """Play an input script. Inputs are `{tick, action}` in replay ticks."""
    by_tick: dict[int, list[str]] = {}
    for item in inputs:
        by_tick.setdefault(int(item["tick"]), []).append(str(item["action"]))
    cube = start.copy() if start is not None else Cube()
    trace: dict[str, list[Any]] = {k: [] for k in TRACE_KEYS}

    def sample(t: int) -> None:
        if record:
            for key, value in zip(trace, (t, cube.x, cube.y, cube.vy, int(cube.grounded),
                                          cube.mode, cube.gravity, int(cube.mini)),
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
            outcome = "unverifiable" if death.cause == "unverifiable" else "death"
            return SimResult(outcome, t, death, trace, cube)
        if until_x is not None and cube.x >= until_x:
            return SimResult("reached", t, None, trace, cube)
    return SimResult("max_ticks", t, None, trace, cube)
