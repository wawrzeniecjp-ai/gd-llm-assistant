"""What each object type *is* in gameplay terms (LEVEL-DESIGN-SPEC.md 4.2).

The source of truth is the game: `describe_object_types` instantiates each id off-scene
and reports its `GameObjectType`, hitbox and flags, and `scripts/dump_roles.py` writes
that to `data/roles.json`. This module maps the game's type enum onto the smaller
`Role` vocabulary the design layer reasons in.

When an id is missing from the table, a role is derived from its sprite frame and
marked `source="heuristic"`. Heuristic roles are good enough to *describe* a level and
never good enough to *verify* one: the simulator treats any span containing a
heuristic gameplay object as unverifiable (P4).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

log = logging.getLogger("sidecar")


class Role(StrEnum):
    SOLID = "solid"
    SLOPE = "slope"
    HAZARD = "hazard"
    ORB = "orb"
    PAD = "pad"
    PORTAL_MODE = "portal_mode"
    PORTAL_SPEED = "portal_speed"
    PORTAL_GRAVITY = "portal_gravity"
    PORTAL_SIZE = "portal_size"
    PORTAL_OTHER = "portal_other"
    TRIGGER = "trigger"
    START_POS = "start_pos"
    COLLECTIBLE = "collectible"
    DECO = "deco"
    SPECIAL = "special"


#: Roles that affect how the level plays. Everything the simulator must model.
GAMEPLAY_ROLES: frozenset[Role] = frozenset(
    {
        Role.SOLID,
        Role.SLOPE,
        Role.HAZARD,
        Role.ORB,
        Role.PAD,
        Role.PORTAL_MODE,
        Role.PORTAL_SPEED,
        Role.PORTAL_GRAVITY,
        Role.PORTAL_SIZE,
        Role.PORTAL_OTHER,
        Role.START_POS,
    }
)

#: `GameObjectType` (Geode bindings, `Enums.hpp`) -> (role, subrole, variant).
#:
#: Keyed by the integer the game reports, so a renamed enum member in a future
#: bindings release cannot silently change a mapping. Types absent from this table
#: map to `Role.SPECIAL` and are logged once.
_GAME_TYPES: dict[int, tuple[Role, str | None, str | None]] = {
    0: (Role.SOLID, "block", None),
    2: (Role.HAZARD, "hazard", None),
    3: (Role.PORTAL_GRAVITY, "gravity", "inverse"),
    4: (Role.PORTAL_GRAVITY, "gravity", "normal"),
    5: (Role.PORTAL_MODE, "mode", "ship"),
    6: (Role.PORTAL_MODE, "mode", "cube"),
    7: (Role.DECO, None, None),
    8: (Role.PAD, "pad", "yellow"),
    9: (Role.PAD, "pad", "pink"),
    10: (Role.PAD, "pad", "blue"),
    11: (Role.ORB, "orb", "yellow"),
    12: (Role.ORB, "orb", "pink"),
    13: (Role.ORB, "orb", "blue"),
    14: (Role.PORTAL_OTHER, "mirror", "on"),
    15: (Role.PORTAL_OTHER, "mirror", "off"),
    16: (Role.PORTAL_MODE, "mode", "ball"),
    17: (Role.PORTAL_SIZE, "size", "normal"),
    18: (Role.PORTAL_SIZE, "size", "mini"),
    19: (Role.PORTAL_MODE, "mode", "ufo"),
    20: (Role.SPECIAL, "modifier", None),
    21: (Role.SPECIAL, "breakable", None),
    22: (Role.COLLECTIBLE, "secret_coin", None),
    23: (Role.PORTAL_OTHER, "dual", "on"),
    24: (Role.PORTAL_OTHER, "dual", "off"),
    25: (Role.SLOPE, "slope", None),
    26: (Role.PORTAL_MODE, "mode", "wave"),
    27: (Role.PORTAL_MODE, "mode", "robot"),
    28: (Role.PORTAL_OTHER, "teleport", None),
    29: (Role.ORB, "orb", "green"),
    30: (Role.COLLECTIBLE, "collectible", None),
    31: (Role.COLLECTIBLE, "user_coin", None),
    32: (Role.ORB, "orb", "black"),
    33: (Role.PORTAL_MODE, "mode", "spider"),
    34: (Role.PAD, "pad", "red"),
    35: (Role.ORB, "orb", "red"),
    36: (Role.ORB, "orb", "custom"),
    37: (Role.ORB, "orb", "dash"),
    38: (Role.ORB, "orb", "gravity_dash"),
    39: (Role.SPECIAL, "collision", None),
    40: (Role.SPECIAL, "special", None),
    41: (Role.PORTAL_MODE, "mode", "swing"),
    42: (Role.PORTAL_GRAVITY, "gravity", "toggle"),
    43: (Role.ORB, "orb", "spider"),
    44: (Role.PAD, "pad", "spider"),
    45: (Role.SPECIAL, "enter_effect", None),
    46: (Role.ORB, "orb", "teleport"),
    47: (Role.HAZARD, "animated_hazard", None),
}

#: Frame-name fragments for the heuristic fallback, checked in order. Conservative on
#: purpose: an unknown frame becomes `SPECIAL`, which the simulator refuses to judge,
#: rather than `DECO`, which it would ignore.
_FRAME_HINTS: tuple[tuple[str, Role], ...] = (
    ("spike", Role.HAZARD),
    ("pit", Role.HAZARD),
    ("saw", Role.HAZARD),
    ("slope", Role.SLOPE),
    ("ring", Role.ORB),
    ("bump", Role.PAD),
    ("portal", Role.PORTAL_OTHER),
    ("boost", Role.PORTAL_SPEED),
    ("block", Role.SOLID),
    ("square", Role.SOLID),
    ("plank", Role.SOLID),
)


@dataclass(frozen=True)
class Hitbox:
    """Relative to the object's centre, at scale 1 and rotation 0."""

    x: float
    y: float
    w: float
    h: float
    radius: float = 0.0

    @property
    def is_circle(self) -> bool:
        return self.radius > 0


@dataclass(frozen=True)
class RoleEntry:
    id: int
    role: Role
    subrole: str | None = None
    variant: str | None = None
    object_type: int | None = None
    hitbox: Hitbox | None = None
    visual: tuple[float, float] | None = None
    has_detail_color: bool = False
    default_z_layer: int | None = None
    frame: str | None = None
    source: str = "game"

    @property
    def is_gameplay(self) -> bool:
        return self.role in GAMEPLAY_ROLES

    @property
    def heuristic(self) -> bool:
        return self.source == "heuristic"


#: Modifiers the type enum cannot tell apart, by object id (S-L2 dump, 2026-11-07).
#:
#: The game gives speed portals and the editor's guide objects the generic `Modifier`
#: type, which on its own maps to SPECIAL: something the simulator refuses to judge.
#: Speed portals are exactly what it must judge, and the guides are editor-only and
#: should not block it. Applied only when the game still reports `Modifier` for the
#: id, so a renumbering in a future version falls back to SPECIAL rather than
#: mislabelling an object.
_MODIFIER_IDS: dict[int, tuple[Role, str | None, str | None]] = {
    200: (Role.PORTAL_SPEED, "speed", "0.5x"),  # boost_01
    201: (Role.PORTAL_SPEED, "speed", "1x"),    # boost_02
    202: (Role.PORTAL_SPEED, "speed", "2x"),    # boost_03
    203: (Role.PORTAL_SPEED, "speed", "3x"),    # boost_04
    1334: (Role.PORTAL_SPEED, "speed", "4x"),   # boost_05
    2016: (Role.DECO, "editor_guide", "camera"),
    3642: (Role.DECO, "editor_guide", "bpm"),
}

#: `GameObjectType.Modifier`.
_MODIFIER = 20


def role_from_description(
    description: dict[str, Any], *, frame: str | None = None
) -> RoleEntry:
    """Build a table entry from one `describe_object_types` entry.

    The flags override the type enum where the game says so: a trigger is a trigger
    whatever its type, and an object the game marks as decoration is deco.
    """
    object_id = int(description["id"])
    object_type = int(description["object_type"])
    role, subrole, variant = _GAME_TYPES.get(object_type, (Role.SPECIAL, None, None))
    if object_type not in _GAME_TYPES:
        log.info("roles: unmapped GameObjectType %d (id %d)", object_type, object_id)

    if description.get("is_trigger"):
        role, subrole, variant = Role.TRIGGER, "trigger", None
    elif object_type == _MODIFIER and object_id in _MODIFIER_IDS:
        role, subrole, variant = _MODIFIER_IDS[object_id]
    elif description.get("is_decoration") and role is Role.SOLID:
        # The type enum's default is Solid (0), so a decoration that never set its
        # type would otherwise read as a collidable block.
        role, subrole, variant = Role.DECO, None, None

    raw_box = description.get("hitbox") or {}
    radius = float(description.get("radius") or 0.0)
    hitbox = Hitbox(
        x=float(raw_box.get("x", 0.0)),
        y=float(raw_box.get("y", 0.0)),
        w=float(raw_box.get("w", 0.0)),
        h=float(raw_box.get("h", 0.0)),
        radius=radius,
    )
    raw_visual = description.get("visual")
    visual = (
        (float(raw_visual["w"]), float(raw_visual["h"]))
        if isinstance(raw_visual, dict) and "w" in raw_visual and "h" in raw_visual
        else None
    )
    return RoleEntry(
        id=object_id,
        role=role,
        subrole=subrole,
        variant=variant,
        object_type=object_type,
        hitbox=hitbox,
        visual=visual,
        has_detail_color=bool(description.get("has_detail_color")),
        default_z_layer=description.get("default_z_layer"),
        frame=frame,
        source="game",
    )


def role_from_frame(object_id: int, frame: str | None) -> RoleEntry:
    """The heuristic fallback, for an id the dumped table does not know."""
    text = (frame or "").lower()
    for fragment, role in _FRAME_HINTS:
        if fragment in text:
            return RoleEntry(id=object_id, role=role, frame=frame, source="heuristic")
    return RoleEntry(id=object_id, role=Role.SPECIAL, frame=frame, source="heuristic")


@dataclass
class RoleTable:
    """Lookup by object id, with the heuristic fallback built in."""

    entries: dict[int, RoleEntry] = field(default_factory=dict)
    game_version: str | None = None
    frames: dict[int, str] = field(default_factory=dict)

    def get(self, object_id: int) -> RoleEntry:
        entry = self.entries.get(object_id)
        if entry is not None:
            return entry
        return role_from_frame(object_id, self.frames.get(object_id))

    def role_of(self, object_id: int) -> Role:
        return self.get(object_id).role

    def __len__(self) -> int:
        return len(self.entries)

    def with_frames(self, frames: dict[int, str]) -> RoleTable:
        """A copy that also knows sprite frames, for better heuristic fallbacks."""
        return RoleTable(entries=dict(self.entries), game_version=self.game_version,
                         frames={**self.frames, **frames})

    # -- persistence -----------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "game_version": self.game_version,
            "entries": [
                _entry_to_json(e) for e in sorted(self.entries.values(), key=lambda e: e.id)
            ],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> RoleTable:
        if raw.get("schema") != 1:
            raise ValueError("roles.json: unsupported schema version")
        entries: dict[int, RoleEntry] = {}
        for item in raw.get("entries") or []:
            entry = _entry_from_json(item)
            entries[entry.id] = entry
        return cls(entries=entries, game_version=raw.get("game_version"))


def _entry_to_json(entry: RoleEntry) -> dict[str, Any]:
    data: dict[str, Any] = {"id": entry.id, "role": entry.role.value}
    if entry.subrole is not None:
        data["subrole"] = entry.subrole
    if entry.variant is not None:
        data["variant"] = entry.variant
    if entry.object_type is not None:
        data["object_type"] = entry.object_type
    if entry.hitbox is not None:
        data["hitbox"] = {
            "x": entry.hitbox.x, "y": entry.hitbox.y,
            "w": entry.hitbox.w, "h": entry.hitbox.h,
        }
        if entry.hitbox.radius:
            data["hitbox"]["radius"] = entry.hitbox.radius
    if entry.visual is not None:
        data["visual"] = {"w": entry.visual[0], "h": entry.visual[1]}
    if entry.has_detail_color:
        data["detail_color"] = True
    if entry.default_z_layer is not None:
        data["default_z_layer"] = entry.default_z_layer
    if entry.frame:
        data["frame"] = entry.frame
    data["source"] = entry.source
    return data


def _entry_from_json(item: dict[str, Any]) -> RoleEntry:
    box = item.get("hitbox")
    visual = item.get("visual")
    return RoleEntry(
        id=int(item["id"]),
        role=Role(item["role"]),
        subrole=item.get("subrole"),
        variant=item.get("variant"),
        object_type=item.get("object_type"),
        hitbox=Hitbox(
            x=float(box["x"]), y=float(box["y"]), w=float(box["w"]), h=float(box["h"]),
            radius=float(box.get("radius", 0.0)),
        ) if isinstance(box, dict) else None,
        visual=(float(visual["w"]), float(visual["h"])) if isinstance(visual, dict) else None,
        has_detail_color=bool(item.get("detail_color", False)),
        default_z_layer=item.get("default_z_layer"),
        frame=item.get("frame"),
        source=str(item.get("source", "game")),
    )


def _default_path() -> Path:
    return Path(str(resources.files("gd_ai_assistant") / "data" / "roles.json"))


#: Entry fields compared by `diff_role_tables`: the ones the design layer acts on.
#: Frame names and visual sizes are left out; they change with art, not gameplay.
_DIFF_FIELDS: tuple[str, ...] = ("role", "subrole", "variant", "object_type", "hitbox")


@dataclass(frozen=True)
class RoleTableDiff:
    """What a re-dump changed, for review before the new table is committed."""

    added: list[int]
    removed: list[int]
    #: (id, field, old value, new value)
    changed: list[tuple[int, str, Any, Any]]

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff_role_tables(old: RoleTable, new: RoleTable) -> RoleTableDiff:
    """Compare two role tables, id by id (docs/UPGRADING.md)."""
    added = sorted(set(new.entries) - set(old.entries))
    removed = sorted(set(old.entries) - set(new.entries))
    changed: list[tuple[int, str, Any, Any]] = []
    for object_id in sorted(set(old.entries) & set(new.entries)):
        before, after = old.entries[object_id], new.entries[object_id]
        for name in _DIFF_FIELDS:
            a, b = getattr(before, name), getattr(after, name)
            if a != b:
                changed.append((object_id, name, a, b))
    return RoleTableDiff(added=added, removed=removed, changed=changed)


#: Guard against a mod that never reports a last page.
_MAX_DESCRIBE_PAGES = 100


async def fetch_role_table(
    link: Any,
    *,
    page_size: int = 200,
    game_version: str | None = None,
    progress: Any = None,
) -> tuple[RoleTable, list[int]]:
    """Build the role table from the running game (`scripts/dump_roles.py`).

    Reads sprite frames through `list_object_catalog` first, so every entry carries the
    frame the game draws, then pages through `describe_object_types`. Returns the
    table and the ids the game refused to instantiate.
    """
    frames: dict[int, str] = {}
    offset: int | None = 0
    for _ in range(_MAX_DESCRIBE_PAGES):
        if offset is None:
            break
        response = await link.request("list_object_catalog", {"offset": offset, "limit": 500})
        if not response.ok:
            raise RuntimeError(f"list_object_catalog failed: {response.error}")
        result = response.result or {}
        for entry in result.get("entries") or []:
            if entry.get("frame"):
                frames[int(entry["id"])] = str(entry["frame"])
        offset = result.get("next_offset")

    entries: dict[int, RoleEntry] = {}
    failed: list[int] = []
    offset = 0
    for _ in range(_MAX_DESCRIBE_PAGES):
        if offset is None:
            break
        response = await link.request(
            "describe_object_types", {"offset": offset, "limit": page_size}
        )
        if not response.ok:
            raise RuntimeError(f"describe_object_types failed: {response.error}")
        result = response.result or {}
        for description in result.get("entries") or []:
            entry = role_from_description(description, frame=frames.get(int(description["id"])))
            entries[entry.id] = entry
        failed.extend(int(i) for i in result.get("failed") or [])
        if progress is not None:
            progress(len(entries) + len(failed), int(result.get("total", 0)))
        offset = result.get("next_offset")

    return RoleTable(entries=entries, game_version=game_version, frames=frames), failed


@lru_cache(maxsize=4)
def load_role_table(path: str | None = None) -> RoleTable:
    """The committed role table, or an empty one when it has not been dumped yet.

    Empty is not an error: every lookup then falls back to the frame heuristic, which
    is enough to describe a level and is marked so it is never used to verify one.
    """
    target = Path(path) if path is not None else _default_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("roles: %s not found; run scripts/dump_roles.py (heuristics only)", target)
        return RoleTable()
    return RoleTable.from_json(raw)
