"""Gameplay objects as collision boxes (LEVEL-DESIGN-SPEC.md 6.6)."""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..level_model import LevelModel
from ..roles import Hitbox, Role, RoleEntry, RoleTable

#: Roles Sim-1 knows how to simulate; any other gameplay role in a span makes it
#: unverifiable.
SIM1_ROLES = frozenset({Role.SOLID, Role.HAZARD, Role.ORB, Role.PAD, Role.PORTAL_SPEED})
SIM1_ORBS = frozenset({"yellow", "pink"})
SIM1_PADS = frozenset({"yellow", "pink"})


@dataclass(frozen=True)
class Box:
    x1: float
    y1: float
    x2: float
    y2: float

    def overlaps(self, other: Box) -> bool:
        return self.x1 < other.x2 and other.x1 < self.x2 and \
            self.y1 < other.y2 and other.y1 < self.y2


@dataclass(frozen=True)
class Body:
    """One object as the simulator sees it."""

    uid: int
    id: int
    role: Role
    variant: str | None
    box: Box
    radius: float = 0.0          # > 0: a circle centred at (cx, cy)
    cx: float = 0.0
    cy: float = 0.0

    def touches(self, player: Box) -> bool:
        if self.radius > 0:
            # Circle against the player's box: closest point distance.
            nx = min(max(self.cx, player.x1), player.x2)
            ny = min(max(self.cy, player.y1), player.y2)
            return math.hypot(self.cx - nx, self.cy - ny) < self.radius
        return self.box.overlaps(player)


def _box(x: float, y: float, hb: Hitbox, rotation: float, sx: float, sy: float,
         flip_x: bool, flip_y: bool) -> Box:
    """The hitbox in level coordinates. Rotation is a multiple of 90 (checked by the
    caller); GD rotates clockwise."""
    x1, y1, x2, y2 = hb.x * sx, hb.y * sy, (hb.x + hb.w) * sx, (hb.y + hb.h) * sy
    if flip_x:
        x1, x2 = -x2, -x1
    if flip_y:
        y1, y2 = -y2, -y1
    for _ in range(int(round(rotation / 90.0)) % 4):
        x1, y1, x2, y2 = y1, -x2, y2, -x1
    return Box(x + x1, y + y1, x + x2, y + y2)


@dataclass
class World:
    """Bodies sorted by their left edge, with the reasons a span cannot be simulated."""

    bodies: list[Body]
    unsupported: list[dict[str, Any]]

    def __post_init__(self) -> None:
        self.bodies.sort(key=lambda b: b.box.x1)
        self._lefts = [b.box.x1 for b in self.bodies]
        self._widest = max((b.box.x2 - b.box.x1 for b in self.bodies), default=0.0)
        # Obstacles only (not speed portals): their left edges and a running maximum of
        # right edges, for `obstacle_between` in O(log n). A speed portal is not a reason
        # to press, and treating it as one let branches cross it at different ticks and
        # split into dozens of x-tracks (a 0.5x section overflowed the beam).
        obstacles = [b for b in self.bodies if b.role is not Role.PORTAL_SPEED]
        self._obstacle_lefts = [b.box.x1 for b in obstacles]
        self._right_max: list[float] = []
        best = float("-inf")
        for body in obstacles:
            best = max(best, body.box.x2)
            self._right_max.append(best)

    def obstacle_between(self, x1: float, x2: float) -> bool:
        """Is any obstacle (anything but a speed portal) within [x1, x2) horizontally?"""
        count = bisect_left(self._obstacle_lefts, x2)
        return count > 0 and self._right_max[count - 1] > x1

    def near(self, x1: float, x2: float) -> list[Body]:
        """Bodies whose box may overlap [x1, x2] horizontally."""
        start = bisect_left(self._lefts, x1 - self._widest)
        out = []
        for body in self.bodies[start:]:
            if body.box.x1 >= x2:
                break
            if body.box.x2 > x1:
                out.append(body)
        return out

    def unsupported_between(self, x1: float, x2: float) -> list[dict[str, Any]]:
        return [u for u in self.unsupported if x1 <= u["x"] <= x2]


def body_for(uid: int, entry: RoleEntry, x: float, y: float, *, rotation: float = 0.0,
             scale_x: float = 1.0, scale_y: float = 1.0, flip_x: bool = False,
             flip_y: bool = False) -> Body | dict[str, Any] | None:
    """A body, an `unsupported` record (dict), or None for something irrelevant."""
    if not entry.is_gameplay or entry.role is Role.START_POS:
        # A start position marks where a playtest begins; it collides with nothing.
        return None
    reason = None
    if entry.role not in SIM1_ROLES:
        reason = f"{entry.role.value} is not simulated yet"
    elif entry.role is Role.ORB and entry.variant not in SIM1_ORBS:
        reason = f"{entry.variant} orbs are not simulated yet"
    elif entry.role is Role.PAD and entry.variant not in SIM1_PADS:
        reason = f"{entry.variant} pads are not simulated yet"
    elif abs(rotation / 90.0 - round(rotation / 90.0)) > 1e-3:
        reason = f"rotation {rotation:g} is not a multiple of 90"
    elif entry.hitbox is None:
        reason = "no hitbox known"
    if reason is not None:
        return {"uid": uid, "id": entry.id, "x": x, "y": y, "reason": reason}
    hb = entry.hitbox
    assert hb is not None
    box = _box(x, y, hb, rotation, scale_x, scale_y, flip_x, flip_y)
    if hb.is_circle:
        return Body(uid, entry.id, entry.role, entry.variant, box,
                    radius=hb.radius * max(scale_x, scale_y), cx=x, cy=y)
    return Body(uid, entry.id, entry.role, entry.variant, box)


def world_from_model(model: LevelModel) -> World:
    bodies: list[Body] = []
    unsupported: list[dict[str, Any]] = []
    for obj in model:
        made = body_for(obj.uid, obj.role_entry, obj.x, obj.y, rotation=obj.rotation,
                        scale_x=obj.scale_x, scale_y=obj.scale_y, flip_x=obj.flip_x,
                        flip_y=obj.flip_y)
        if isinstance(made, Body):
            bodies.append(made)
        elif made is not None:
            unsupported.append(made)
    return World(bodies, unsupported)


def world_from_objects(objects: Iterable[Mapping[str, Any]], roles: RoleTable) -> World:
    """From `create_objects`-style payloads (used by the golden tests)."""
    bodies: list[Body] = []
    unsupported: list[dict[str, Any]] = []
    for uid, obj in enumerate(objects, start=1):
        props = obj.get("properties") or {}
        made = body_for(uid, roles.get(int(obj["id"])), float(obj["x"]), float(obj["y"]),
                        rotation=float(props.get("rotation", 0.0)),
                        scale_x=float(props.get("scale_x", 1.0)),
                        scale_y=float(props.get("scale_y", 1.0)),
                        flip_x=bool(props.get("flip_x", False)),
                        flip_y=bool(props.get("flip_y", False)))
        if isinstance(made, Body):
            bodies.append(made)
        elif made is not None:
            unsupported.append(made)
    return World(bodies, unsupported)
