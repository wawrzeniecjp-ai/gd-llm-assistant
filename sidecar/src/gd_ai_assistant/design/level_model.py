"""A read-only snapshot of the level (LEVEL-DESIGN-SPEC.md 4.3).

The design layer reasons about the level as a whole -- a section's gameplay, the
solids around a hazard, the deco near an edge -- which a paged `query_objects` cannot
serve. A `LevelModel` is the whole level read once through `get_level_objects`, with a
spatial index on top.

It lives for one tool-call sequence at most (LD2). `SnapshotCache` holds it within a
turn and drops it on any mutation or hand edit, so nothing about level content
outlives the turn -- SPEC.md D4 (no mirror) still holds.

**Consistency.** The level is read in pages, and the user can edit between them. Every
page carries the mod's level revision; a snapshot is accepted only when every page has
the same one. On a mismatch the read restarts once; a second mismatch raises
`LevelChangingError`, which tools report as "the user is editing".
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from .roles import GAMEPLAY_ROLES, Role, RoleEntry, RoleTable

log = logging.getLogger("sidecar")

#: Objects per `get_level_objects` page. The mod's maximum is 2000.
DEFAULT_PAGE_SIZE = 1000

#: Guard against a mod that never reports a last page.
MAX_PAGES = 200

#: Width of one spatial-index bucket, in GD units: ten grid columns.
BUCKET_WIDTH = 300.0


class LevelChangingError(RuntimeError):
    """The level kept changing while it was being read."""


class SnapshotError(RuntimeError):
    """The mod refused or failed a snapshot request."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------


def parse_save_string(text: str) -> dict[int, str]:
    """`k,v,k,v,...` -> {key: value}. Malformed pairs are skipped, not guessed."""
    parts = text.split(",")
    pairs: dict[int, str] = {}
    for index in range(0, len(parts) - 1, 2):
        key_text = parts[index].strip()
        if not key_text.lstrip("-").isdigit():
            continue
        pairs[int(key_text)] = parts[index + 1]
    return pairs


@dataclass(frozen=True)
class LevelObject:
    """One object, with its named properties decoded and its save string kept raw."""

    uid: int
    type_id: int
    x: float
    y: float
    rotation: float
    scale_x: float
    scale_y: float
    flip_x: bool
    flip_y: bool
    z_layer: str | int | None
    z_order: int
    editor_layer: int
    groups: tuple[int, ...]
    color_1: int | None
    color_2: int | None
    role: Role
    role_entry: RoleEntry
    raw: Mapping[int, str] = field(repr=False, default_factory=dict)

    @property
    def is_gameplay(self) -> bool:
        return self.role in GAMEPLAY_ROLES

    @property
    def rotation_is_axis_aligned(self) -> bool:
        """True for multiples of 90 degrees, the only rotations collision handles (LD6)."""
        return math.isclose(self.rotation % 90.0, 0.0, abs_tol=1e-3) or math.isclose(
            self.rotation % 90.0, 90.0, abs_tol=1e-3
        )

    @classmethod
    def from_record(cls, record: Mapping[str, Any], roles: RoleTable) -> LevelObject:
        """Build from one `get_level_objects` record (schema `LevelObjectRecord`)."""
        props = record.get("properties") or {}
        type_id = int(record["id"])
        entry = roles.get(type_id)
        return cls(
            uid=int(record["object_id"]),
            type_id=type_id,
            x=float(props.get("x", 0.0)),
            y=float(props.get("y", 0.0)),
            rotation=float(props.get("rotation", 0.0)),
            scale_x=float(props.get("scale_x", 1.0)),
            scale_y=float(props.get("scale_y", 1.0)),
            flip_x=bool(props.get("flip_x", False)),
            flip_y=bool(props.get("flip_y", False)),
            z_layer=props.get("z_layer"),
            z_order=int(props.get("z_order", 0)),
            editor_layer=int(props.get("editor_layer", 0)),
            groups=tuple(int(g) for g in props.get("groups") or ()),
            color_1=_optional_int(props.get("color_channel_1")),
            color_2=_optional_int(props.get("color_channel_2")),
            role=entry.role,
            role_entry=entry,
            raw=MappingProxyType(parse_save_string(str(record.get("save", "")))),
        )


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    x1: float
    y1: float
    x2: float
    y2: float

    def contains(self, x: float, y: float) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2


class LevelModel:
    """An immutable view of the level at one revision."""

    def __init__(
        self,
        objects: Iterable[LevelObject],
        *,
        revision: int,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        self._objects: tuple[LevelObject, ...] = tuple(objects)
        self.revision = revision
        self.settings: Mapping[str, Any] = MappingProxyType(dict(settings or {}))
        self._by_uid = {obj.uid: obj for obj in self._objects}
        buckets: dict[int, list[LevelObject]] = defaultdict(list)
        for obj in self._objects:
            buckets[_bucket(obj.x)].append(obj)
        self._buckets: dict[int, tuple[LevelObject, ...]] = {
            key: tuple(value) for key, value in buckets.items()
        }

    # -- access ------------------------------------------------------------

    @property
    def objects(self) -> Sequence[LevelObject]:
        return self._objects

    def __len__(self) -> int:
        return len(self._objects)

    def __iter__(self) -> Iterator[LevelObject]:
        return iter(self._objects)

    def by_uid(self, uid: int) -> LevelObject | None:
        return self._by_uid.get(uid)

    def in_rect(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        roles: Iterable[Role] | None = None,
    ) -> list[LevelObject]:
        """Objects whose **centre** lies in the rectangle (inclusive), sorted by x then y."""
        rect = Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        wanted = frozenset(roles) if roles is not None else None
        found: list[LevelObject] = []
        for key in range(_bucket(rect.x1), _bucket(rect.x2) + 1):
            for obj in self._buckets.get(key, ()):
                if wanted is not None and obj.role not in wanted:
                    continue
                if rect.contains(obj.x, obj.y):
                    found.append(obj)
        found.sort(key=lambda o: (o.x, o.y, o.uid))
        return found

    def gameplay(self) -> list[LevelObject]:
        return [obj for obj in self._objects if obj.is_gameplay]

    def bounds(self, *, roles: Iterable[Role] | None = None) -> Rect | None:
        wanted = frozenset(roles) if roles is not None else None
        chosen = [o for o in self._objects if wanted is None or o.role in wanted]
        if not chosen:
            return None
        return Rect(
            min(o.x for o in chosen),
            min(o.y for o in chosen),
            max(o.x for o in chosen),
            max(o.y for o in chosen),
        )

    def heuristic_roles(self) -> list[LevelObject]:
        """Gameplay-looking objects whose role came from a frame name, not the game."""
        return [o for o in self._objects if o.role_entry.heuristic and o.role is not Role.DECO]


def _bucket(x: float) -> int:
    return math.floor(x / BUCKET_WIDTH)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class _Response(Protocol):
    ok: bool
    result: dict[str, Any] | None
    error: dict[str, Any] | None


class _Link(Protocol):
    async def request(self, type_name: str, payload: dict[str, Any] | None = None) -> Any: ...


async def load_level_model(
    link: _Link,
    roles: RoleTable,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    attempts: int = 2,
    settings: Mapping[str, Any] | None = None,
) -> LevelModel:
    """Read the whole level through `get_level_objects`.

    Raises `LevelChangingError` when the level changes during every attempt, and
    `SnapshotError` for any other refusal -- including `UNSUPPORTED_REQUEST` from a
    mod that predates the request, which callers turn into "this tool is unavailable".
    """
    for attempt in range(1, attempts + 1):
        try:
            return await _read_once(link, roles, page_size, settings)
        except _RevisionMoved as moved:
            log.info("snapshot: level changed during read (attempt %d): %s", attempt, moved)
    raise LevelChangingError(
        "the level kept changing while it was being read; the user is probably editing"
    )


class _RevisionMoved(Exception):
    pass


async def _read_once(
    link: _Link,
    roles: RoleTable,
    page_size: int,
    settings: Mapping[str, Any] | None,
) -> LevelModel:
    objects: list[LevelObject] = []
    revision: int | None = None
    cursor: str | None = None

    for _ in range(MAX_PAGES):
        payload: dict[str, Any] = {"limit": page_size}
        if cursor is not None:
            payload["cursor"] = cursor
        response = await link.request("get_level_objects", payload)
        if not response.ok:
            error = response.error or {}
            code = error.get("code")
            if code == "LEVEL_CHANGING":
                raise _RevisionMoved(error.get("message", "level changing"))
            raise SnapshotError(error.get("message", "get_level_objects failed"), code)

        result = response.result if isinstance(response.result, dict) else {}
        page_revision = result.get("revision")
        if not isinstance(page_revision, int):
            raise SnapshotError("get_level_objects returned no revision", "INTERNAL_ERROR")
        if revision is None:
            revision = page_revision
        elif page_revision != revision:
            raise _RevisionMoved(f"revision {revision} -> {page_revision}")

        for record in result.get("objects") or []:
            if isinstance(record, dict):
                objects.append(LevelObject.from_record(record, roles))

        next_cursor = result.get("next_cursor")
        if not isinstance(next_cursor, str):
            break
        cursor = next_cursor
    else:
        raise SnapshotError("get_level_objects never reported a last page", "INTERNAL_ERROR")

    assert revision is not None
    return LevelModel(objects, revision=revision, settings=settings)


# ---------------------------------------------------------------------------
# Per-turn cache
# ---------------------------------------------------------------------------


class SnapshotCache:
    """At most one snapshot, dropped on any mutation (LEVEL-DESIGN-SPEC.md 4.3.3)."""

    def __init__(self, link: _Link, roles: RoleTable) -> None:
        self._link = link
        self._roles = roles
        self._model: LevelModel | None = None
        self.loads = 0

    @property
    def roles(self) -> RoleTable:
        return self._roles

    async def get(self) -> LevelModel:
        if self._model is None:
            self._model = await load_level_model(self._link, self._roles)
            self.loads += 1
        return self._model

    def invalidate(self) -> None:
        self._model = None

    @property
    def cached(self) -> bool:
        return self._model is not None
