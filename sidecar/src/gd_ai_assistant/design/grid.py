"""GridText: the model's working language for gameplay (LEVEL-DESIGN-SPEC.md 5).

A GridText block is a header and rows of characters, one character per 30-unit cell, top
row first::

    @grid col=120 row=0 w=24 h=6
    ........................
    ..............##........
    .........o....##....^^..
    ....^.........##...####.
    ########################

This module is pure: it parses and emits the text, resolves characters to objects
(`plan_placement`, for writing) and objects to characters (`render`, for reading). The
tools in `grid_tools.py` connect it to the editor.

Two rules carry most of the weight:

- **Nothing is silently snapped.** An object that is off its cell centre, rotated by a
  non-right angle or scaled is shown as `?` and listed in `extras` with the reason.
- **What is read can be written back unchanged.** Ids without a built-in character get a
  custom character for that response, with the legend entry that recreates them exactly.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .coords import CELL, Grid
from .level_model import LevelModel, LevelObject
from .roles import GAMEPLAY_ROLES, Hitbox, Role, RoleTable

EMPTY = "."
CONTINUATION = ":"
UNKNOWN = "?"
COMMENT = ";"

#: An object within this many units of its cell centre (after its snap offset) is on the
#: grid; further away it is shown as `?` (5.3).
TOLERANCE = 3.0

#: An object read as flush with a cell edge (`align`) must be within this of that spot.
_ALIGN_TOLERANCE = 0.5

#: Per-call limits for `render_grid` (5.3).
MAX_COLS = 200
MAX_ROWS = 40


class GridError(ValueError):
    """A grid or legend the model wrote is invalid. Every problem is listed, not just one."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# -- legend ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Builtin:
    """A built-in character: a role, and the default object that plays it (5.2.1)."""

    char: str
    role: Role
    variant: str | None
    id: int
    rotation: int = 0
    #: Where in the cell the object sits: the cell centre, or the bottom/top half.
    placement: str = "centre"


def _b(char: str, role: Role, variant: str | None, object_id: int, rotation: int = 0,
       placement: str = "centre") -> Builtin:
    return Builtin(char, role, variant, object_id, rotation, placement)


#: The built-in legend. Characters are LOCKED by the spec; the ids are DEFAULT and a test
#: checks each one against `roles.json`.
BUILTINS: tuple[Builtin, ...] = (
    _b("#", Role.SOLID, None, 1),
    _b("=", Role.SOLID, None, 40, placement="bottom"),
    _b("-", Role.SOLID, None, 40, placement="top"),
    _b("^", Role.HAZARD, None, 8, 0),
    _b(">", Role.HAZARD, None, 8, 90),
    _b("v", Role.HAZARD, None, 8, 180),
    _b("<", Role.HAZARD, None, 8, 270),
    _b(",", Role.HAZARD, None, 39, placement="bottom"),
    _b("*", Role.HAZARD, None, 88),
    _b("o", Role.ORB, "yellow", 36),
    _b("p", Role.ORB, "pink", 141),
    _b("b", Role.ORB, "blue", 84),
    _b("g", Role.ORB, "green", 1022),
    _b("r", Role.ORB, "red", 1333),
    _b("k", Role.ORB, "black", 1330),
    _b("d", Role.ORB, "dash", 1704),
    _b("_", Role.PAD, "yellow", 35, placement="bottom"),
    _b("~", Role.PAD, "pink", 140, placement="bottom"),
    _b("!", Role.PAD, "blue", 67, placement="bottom"),
    _b("C", Role.PORTAL_MODE, "cube", 12),
    _b("S", Role.PORTAL_MODE, "ship", 13),
    _b("B", Role.PORTAL_MODE, "ball", 47),
    _b("U", Role.PORTAL_MODE, "ufo", 111),
    _b("W", Role.PORTAL_MODE, "wave", 660),
    _b("R", Role.PORTAL_MODE, "robot", 745),
    _b("I", Role.PORTAL_MODE, "spider", 1331),
    _b("G", Role.PORTAL_MODE, "swing", 1933),
    _b("1", Role.PORTAL_SPEED, "0.5x", 200),
    _b("2", Role.PORTAL_SPEED, "1x", 201),
    _b("3", Role.PORTAL_SPEED, "2x", 202),
    _b("4", Role.PORTAL_SPEED, "3x", 203),
    _b("5", Role.PORTAL_SPEED, "4x", 1334),
    _b("+", Role.PORTAL_GRAVITY, "inverse", 11),
    _b("&", Role.PORTAL_GRAVITY, "normal", 10),
    _b("n", Role.PORTAL_SIZE, "normal", 99),
    _b("N", Role.PORTAL_SIZE, "mini", 101),
    _b("@", Role.START_POS, None, 31),
)
BUILTIN_BY_CHAR: dict[str, Builtin] = {b.char: b for b in BUILTINS}

#: Characters a custom legend may define (5.2.1): `0`, `6`-`9`, the lowercase letters the
#: built-in legend does not use, and a fixed set of symbols.
CUSTOM_CHARS: str = (
    "06789"
    + "".join(c for c in "abcdefghijklmnopqrstuvwxyz" if c not in BUILTIN_BY_CHAR)
    + "$%()[]{}|/\\'\""
)

#: Snap offset per built-in character, in units from the cell centre: where the editor's
#: own grid placement puts that object (5.2.3, spike S-L9). Characters not listed sit at
#: the centre (the orb was measured as the control: 0). Measured 2026-11-07 with
#: `scripts/check_snap.py` by placing each object with a normal click on the ground row:
#: every one sits on the floor of its cell, so the offset is minus (half the cell minus
#: half the object's height).
_SNAP_BY_CHAR: dict[str, tuple[float, float] | None] = {
    ",": (0.0, -9.0),    # small spike, id 39
    "_": (0.0, -13.0),   # yellow pad, id 35
    "~": (0.0, -13.0),   # pink pad, id 140
    "!": (0.0, -12.0),   # blue pad, id 67 (a little taller than the others)
    # Half slabs (planks, ids 40/1903/147/1904/215, all 14 tall): the editor puts them at
    # the TOP of the cell, +8 (measured on five planks). The bottom-of-cell slab is not
    # an editor default; its offset is derived from the hitbox: cell bottom -15, plus half
    # the plank's 14-unit height, is -8.
    "-": (0.0, 8.0),
    "=": (0.0, -8.0),
}
#: Characters whose offset is not measured: placed at the centre with a note, or refused
#: when the offset is None. Empty since S-L9 was measured; kept for the next new character.
_UNMEASURED_CHARS: frozenset[str] = frozenset()


def _snap(builtin: Builtin | None) -> tuple[float, float] | None:
    """The offset from the cell centre for a character; None if it cannot be written."""
    if builtin is None:
        return (0.0, 0.0)
    return _SNAP_BY_CHAR.get(builtin.char, (0.0, 0.0))

#: Keys a custom legend entry may set, besides `id` (the settable properties, SPEC 7.5).
_GLYPH_PROPERTY_KEYS = frozenset({
    "z_layer", "z_order", "editor_layer", "groups", "color_channel_1", "color_channel_2",
})


@dataclass(frozen=True)
class Glyph:
    """Exactly what one character places: an id and its transform and properties."""

    id: int
    rotation: int = 0
    flip_x: bool = False
    flip_y: bool = False
    properties: tuple[tuple[str, Any], ...] = ()
    #: Which cell edge the object's hitbox sits flush against (`ALIGNMENTS`), or None for
    #: the cell centre. How the editor places a half-height block: at the top of its cell.
    align: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id}
        if self.align:
            data["align"] = self.align
        if self.rotation:
            data["rotation"] = self.rotation
        if self.flip_x:
            data["flip_x"] = True
        if self.flip_y:
            data["flip_y"] = True
        if self.properties:
            data["properties"] = {k: _thaw(v) for k, v in self.properties}
        return data

    def object_properties(self) -> dict[str, Any]:
        """The `create_objects` property set for this glyph."""
        props: dict[str, Any] = {k: _thaw(v) for k, v in self.properties}
        if self.rotation:
            props["rotation"] = self.rotation
        if self.flip_x:
            props["flip_x"] = True
        if self.flip_y:
            props["flip_y"] = True
        return props


def _freeze(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


def _thaw(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


ALIGNMENTS = ("top", "bottom", "left", "right")


def align_offset(glyph: Glyph, hitbox: Hitbox | None) -> tuple[float, float] | None:
    """The offset from the cell centre that puts the hitbox flush with `glyph.align`.

    Computed from the rotated hitbox, so it reproduces what S-L9 measured: a 14-unit
    plank at the top of its cell is +8, a 15-unit half block +7.5. None when the object
    has no usable hitbox (the alignment cannot be honoured, so it is refused).
    """
    if glyph.align is None:
        return (0.0, 0.0)
    if hitbox is None or hitbox.is_circle or hitbox.w <= 0 or hitbox.h <= 0:
        return None
    x1, y1, x2, y2 = _rotated_box(hitbox, glyph.rotation)
    half = CELL / 2
    return {
        "top": (0.0, half - y2),
        "bottom": (0.0, -half - y1),
        "left": (-half - x1, 0.0),
        "right": (half - x2, 0.0),
    }[glyph.align]


def _builtin_glyph(builtin: Builtin) -> Glyph:
    return Glyph(id=builtin.id, rotation=builtin.rotation)


def _normal_rotation(value: float) -> int | None:
    """The rotation as 0/90/180/270, or None when it is not a right angle."""
    turns = value / 90.0
    if abs(turns - round(turns)) > 1e-3:
        return None
    return int(round(turns)) % 4 * 90


def parse_legend(raw: Mapping[str, Any] | None) -> dict[str, Glyph]:
    """Validate a custom legend (5.2.2). Raises `GridError` listing every bad entry."""
    if not raw:
        return {}
    if not isinstance(raw, Mapping):
        raise GridError(["legend must be an object mapping characters to entries"])
    problems: list[str] = []
    legend: dict[str, Glyph] = {}
    for char, entry in raw.items():
        where = f"legend {char!r}"
        if char in BUILTIN_BY_CHAR:
            problems.append(f"{where}: built-in characters cannot be redefined")
            continue
        if len(char) != 1 or char not in CUSTOM_CHARS:
            problems.append(f"{where}: custom characters must be one of {CUSTOM_CHARS}")
            continue
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), int):
            problems.append(f"{where}: needs an integer 'id'")
            continue
        unknown = set(entry) - {"id", "rotation", "flip_x", "flip_y", "properties", "align"}
        if unknown:
            problems.append(f"{where}: unknown keys {sorted(unknown)}")
            continue
        rotation = _normal_rotation(float(entry.get("rotation", 0)))
        if rotation is None:
            problems.append(f"{where}: rotation must be a multiple of 90")
            continue
        align = entry.get("align")
        if align is not None and align not in ALIGNMENTS:
            problems.append(f"{where}: align must be one of {list(ALIGNMENTS)}")
            continue
        props = entry.get("properties") or {}
        if not isinstance(props, Mapping) or set(props) - _GLYPH_PROPERTY_KEYS:
            problems.append(
                f"{where}: properties may only set {sorted(_GLYPH_PROPERTY_KEYS)}"
            )
            continue
        legend[char] = Glyph(
            id=entry["id"],
            rotation=rotation,
            flip_x=bool(entry.get("flip_x", False)),
            flip_y=bool(entry.get("flip_y", False)),
            properties=tuple(sorted((k, _freeze(v)) for k, v in props.items())),
            align=align,
        )
    if problems:
        raise GridError(problems)
    return legend


# -- text format ---------------------------------------------------------------------------

_HEADER = re.compile(r"^@grid((?:\s+[a-z]+=-?\d+)+)\s*$")
_HEADER_KEYS = ("col", "row", "w", "h")


@dataclass(frozen=True)
class GridBlock:
    """A parsed GridText block. `rows` are top first, as written."""

    col: int
    row: int
    w: int
    h: int
    rows: tuple[str, ...]
    #: (number of cell rows written before the comment, comment text including ';')
    comments: tuple[tuple[int, str], ...] = ()

    @property
    def top_row(self) -> int:
        return self.row + self.h - 1

    def char_at(self, col: int, row: int) -> str:
        """The character at absolute grid coordinates (must be inside the block)."""
        return self.rows[self.top_row - row][col - self.col]

    def contains(self, col: int, row: int) -> bool:
        return self.col <= col < self.col + self.w and self.row <= row <= self.top_row

    def on_border(self, col: int, row: int) -> bool:
        return col in (self.col, self.col + self.w - 1) or row in (self.row, self.top_row)

    def cells(self) -> Iterator[tuple[int, int, str]]:
        """Every non-empty cell as (col, row, char), top row first, left to right."""
        for index, line in enumerate(self.rows):
            row = self.top_row - index
            for offset, char in enumerate(line):
                if char != EMPTY:
                    yield self.col + offset, row, char


def parse(text: str) -> GridBlock:
    """Parse one GridText block. Raises `GridError` with every problem found."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # a single trailing newline is allowed
    lines = [line.rstrip("\r") for line in lines]
    if not lines:
        raise GridError(["empty grid: expected a '@grid col=.. row=.. w=.. h=..' header"])

    match = _HEADER.match(lines[0].strip())
    if not match:
        raise GridError([f"bad header {lines[0]!r}: expected '@grid col=C row=R w=W h=H'"])
    values: dict[str, int] = {}
    problems: list[str] = []
    for part in match.group(1).split():
        key, _, number = part.partition("=")
        if key not in _HEADER_KEYS:
            problems.append(f"header: unknown key {key!r}")
        elif key in values:
            problems.append(f"header: {key} given twice")
        else:
            values[key] = int(number)
    missing = [k for k in _HEADER_KEYS if k not in values]
    if missing:
        problems.append(f"header: missing {', '.join(missing)}")
    if problems:
        raise GridError(problems)
    width, height = values["w"], values["h"]
    if width < 1 or height < 1:
        raise GridError([f"header: w and h must be at least 1 (got w={width} h={height})"])

    rows: list[str] = []
    comments: list[tuple[int, str]] = []
    allowed = set(EMPTY + CONTINUATION + UNKNOWN) | set(BUILTIN_BY_CHAR) | set(CUSTOM_CHARS)
    for number, line in enumerate(lines[1:], start=2):
        if line.startswith(COMMENT):
            comments.append((len(rows), line))
            continue
        if len(line) != width:
            problems.append(f"line {number}: {len(line)} characters, header says w={width}")
        bad = sorted({c for c in line if c not in allowed})
        if bad:
            problems.append(f"line {number}: unknown characters {''.join(bad)!r}")
        rows.append(line)
    if len(rows) != height:
        problems.append(f"{len(rows)} rows, header says h={height}")
    if problems:
        raise GridError(problems)
    return GridBlock(values["col"], values["row"], width, height, tuple(rows), tuple(comments))


def emit(block: GridBlock) -> str:
    """The canonical text of a block: no trailing newline, header keys in spec order."""
    out = [f"@grid col={block.col} row={block.row} w={block.w} h={block.h}"]
    pending = list(block.comments)
    for index, line in enumerate(block.rows):
        while pending and pending[0][0] == index:
            out.append(pending.pop(0)[1])
        out.append(line)
    out.extend(text for _, text in pending)
    return "\n".join(out)


# -- footprints --------------------------------------------------------------------------


def _rotated_box(hitbox: Hitbox, rotation: int) -> tuple[float, float, float, float]:
    """(x1, y1, x2, y2) of the hitbox around the object centre, after a right-angle turn.

    GD rotates clockwise for positive angles.
    """
    x1, y1, x2, y2 = hitbox.x, hitbox.y, hitbox.x + hitbox.w, hitbox.y + hitbox.h
    for _ in range((rotation // 90) % 4):
        x1, y1, x2, y2 = y1, -x2, y2, -x1
    return x1, y1, x2, y2


def footprint(hitbox: Hitbox | None, rotation: int = 0) -> tuple[tuple[int, int], ...]:
    """Cells, relative to the anchor, that the object's hitbox covers besides its own.

    A cell is covered when its centre lies strictly inside the hitbox (or circle). A
    portal 86 units tall covers the cells above and below; an orb or a spike covers none.
    """
    if hitbox is None:
        return ()
    covered: list[tuple[int, int]] = []
    if hitbox.is_circle:
        reach = int(hitbox.radius // CELL)
        for dc in range(-reach, reach + 1):
            for dr in range(-reach, reach + 1):
                if (dc or dr) and math.hypot(dc * CELL, dr * CELL) < hitbox.radius:
                    covered.append((dc, dr))
        return tuple(covered)
    x1, y1, x2, y2 = _rotated_box(hitbox, rotation)
    for dc in range(math.floor(x1 / CELL) - 1, math.ceil(x2 / CELL) + 2):
        for dr in range(math.floor(y1 / CELL) - 1, math.ceil(y2 / CELL) + 2):
            cx, cy = dc * CELL, dr * CELL
            if (dc or dr) and x1 < cx < x2 and y1 < cy < y2:
                covered.append((dc, dr))
    return tuple(covered)


# -- writing ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedObject:
    char: str
    col: int
    row: int
    glyph: Glyph

    def payload(self, grid: Grid, offset: tuple[float, float]) -> dict[str, Any]:
        dx, dy = offset
        data: dict[str, Any] = {
            "id": self.glyph.id,
            "x": grid.centre_x(self.col) + dx,
            "y": grid.centre_y(self.row) + dy,
        }
        props = self.glyph.object_properties()
        if props:
            data["properties"] = props
        return data


@dataclass
class Placement:
    """What a block would create: one entry per anchor cell, in reading order."""

    objects: list[PlannedObject]
    payloads: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)

    @property
    def cells(self) -> set[tuple[int, int]]:
        return {(o.col, o.row) for o in self.objects}


def plan_placement(
    block: GridBlock, legend: Mapping[str, Glyph], grid: Grid, roles: RoleTable
) -> Placement:
    """Resolve every cell of a block to the object it creates (5.4).

    Raises `GridError` listing every problem: unknown characters, `?` cells, placements
    that cannot be written yet, and `:` cells with no anchor that could cover them.
    """
    problems: list[str] = []
    objects: list[PlannedObject] = []
    payloads: list[dict[str, Any]] = []
    unmeasured_used: set[str] = set()
    continuations: list[tuple[int, int]] = []

    for col, row, char in block.cells():
        where = f"cell ({col},{row}) {char!r}"
        if char == CONTINUATION:
            continuations.append((col, row))
            continue
        if char == UNKNOWN:
            problems.append(f"{where}: '?' cannot be written; use a custom legend entry "
                            "for that object or leave the cell empty")
            continue
        builtin = BUILTIN_BY_CHAR.get(char)
        if builtin is not None:
            glyph, offset = _builtin_glyph(builtin), _snap(builtin)
        elif char in legend:
            glyph = legend[char]
            entry = roles.get(glyph.id)
            offset = align_offset(glyph, entry.hitbox if entry else None)
            if offset is None:
                problems.append(f"{where}: align needs an object with a box hitbox")
                continue
        else:
            problems.append(f"{where}: custom character with no legend entry")
            continue
        if offset is None:
            problems.append(f"{where}: where the editor puts this in its cell is not measured "
                            "yet (spike S-L9); use '=' or a custom legend entry instead")
            continue
        if char in _UNMEASURED_CHARS:
            unmeasured_used.add(char)
        planned = PlannedObject(char, col, row, glyph)
        objects.append(planned)
        payloads.append(planned.payload(grid, offset))

    covered: set[tuple[int, int]] = set()
    for planned in objects:
        entry = roles.get(planned.glyph.id)
        for dc, dr in footprint(entry.hitbox if entry else None, planned.glyph.rotation):
            covered.add((planned.col + dc, planned.row + dr))
    for col, row in continuations:
        if (col, row) not in covered and not block.on_border(col, row):
            problems.append(f"cell ({col},{row}) ':': no object in this block covers it "
                            "(check the anchor is in the right cell)")

    if problems:
        raise GridError(problems)
    notes = [
        f"'{c}' objects are placed at the cell centre: where the editor puts them in the "
        "cell is not measured yet (spike S-L9)"
        for c in sorted(unmeasured_used)
    ]
    return Placement(objects=objects, payloads=payloads, notes=notes)


# -- reading -----------------------------------------------------------------------------

#: Which object wins a cell two gameplay objects share (5.3): lower is stronger.
_PRIORITY: dict[Role, int] = {
    Role.HAZARD: 0,
    Role.ORB: 1,
    Role.PAD: 2,
    Role.PORTAL_MODE: 3,
    Role.PORTAL_SPEED: 3,
    Role.PORTAL_GRAVITY: 3,
    Role.PORTAL_SIZE: 3,
    Role.PORTAL_OTHER: 3,
    Role.START_POS: 4,
    Role.SOLID: 5,
    Role.SLOPE: 6,
}

LAYER_ROLES: dict[str, frozenset[Role]] = {
    "gameplay": GAMEPLAY_ROLES,
    "triggers": frozenset({Role.TRIGGER}),
    "deco": frozenset({Role.DECO}),
}


@dataclass
class Rendered:
    block: GridBlock
    #: char -> {"id", "count", ...glyph fields for custom characters}
    legend_used: dict[str, dict[str, Any]]
    extras: list[dict[str, Any]]
    overlaps: list[dict[str, Any]]
    notes: list[str]

    @property
    def custom_legend(self) -> dict[str, dict[str, Any]]:
        """The legend `place_grid` needs to recreate this block's custom characters."""
        return {
            char: {k: v for k, v in used.items() if k != "count"}
            for char, used in self.legend_used.items()
            if char not in BUILTIN_BY_CHAR
        }


def _glyph_of(obj: LevelObject) -> tuple[Glyph | None, str | None]:
    """The glyph that recreates `obj`, or None and the reason it cannot be represented."""
    rotation = _normal_rotation(obj.rotation)
    if rotation is None:
        return None, f"rotation {obj.rotation:g} is not a multiple of 90"
    if abs(obj.scale_x - 1) > 1e-3 or abs(obj.scale_y - 1) > 1e-3:
        return None, f"scaled {obj.scale_x:g}x{obj.scale_y:g}"
    return Glyph(id=obj.type_id, rotation=rotation, flip_x=obj.flip_x, flip_y=obj.flip_y), None


def _builtins_for(glyph: Glyph) -> list[Builtin]:
    """Built-in characters that place exactly this glyph. Several can: `=` and `-` are
    the same plank at different heights, so the reader picks by position."""
    if glyph.flip_x or glyph.flip_y or glyph.properties or glyph.align:
        return []
    # A character whose offset is unknown (None) can be neither matched nor written.
    return [b for b in BUILTINS if b.id == glyph.id and b.rotation == glyph.rotation
            and _snap(b) is not None]


def render(
    model: LevelModel,
    grid: Grid,
    *,
    col: int,
    row: int,
    w: int,
    h: int,
    layers: Iterable[str] = ("gameplay",),
) -> Rendered:
    """Draw the level's cells `col..col+w-1` x `row..row+h-1` as GridText (5.3)."""
    wanted: set[Role] = set()
    for layer in layers:
        wanted |= LAYER_ROLES[layer]

    margin = 3 * CELL  # footprints reach at most a couple of cells
    x1, x2 = col * CELL - margin, (col + w) * CELL + margin
    y1 = grid.ground_top_y + row * CELL - margin
    y2 = grid.ground_top_y + (row + h) * CELL + margin
    candidates = model.in_rect(x1, y1, x2, y2, roles=wanted)

    anchors: dict[tuple[int, int], list[tuple[LevelObject, Glyph, Builtin | None]]] = {}
    unknown: dict[tuple[int, int], list[LevelObject]] = {}
    covered: set[tuple[int, int]] = set()
    extras: list[dict[str, Any]] = []

    for obj in candidates:
        glyph, reason = _glyph_of(obj)
        # Try each character that could place this object, then a custom character at the
        # cell centre, then one flush with each cell edge (how the editor places half
        # blocks); keep the first that lands within its tolerance (else the first tried,
        # which reports how far off it is). An edge alignment must match exactly: with the
        # centre's +-3 slop, a spike half a cell off the grid passed as "flush left".
        options: list[tuple[Builtin | None, Glyph | None, tuple[float, float], float]] = []
        if glyph is not None:
            for option in _builtins_for(glyph):
                options.append((option, glyph, _snap(option) or (0.0, 0.0), TOLERANCE))
            options.append((None, glyph, (0.0, 0.0), TOLERANCE))
            for edge in ALIGNMENTS:
                aligned = replace(glyph, align=edge)
                offset = align_offset(aligned, obj.role_entry.hitbox)
                if offset is not None and offset != (0.0, 0.0):
                    options.append((None, aligned, offset, _ALIGN_TOLERANCE))
        else:
            options.append((None, None, (0.0, 0.0), TOLERANCE))
        fits = []
        for option, variant, (dx, dy), tolerance in options:
            cell = grid.cell_of(obj.x - dx, obj.y - dy)
            ox = obj.x - dx - grid.centre_x(cell.col)
            oy = obj.y - dy - grid.centre_y(cell.row)
            fits.append((option, variant, cell, ox, oy))
            if abs(ox) <= tolerance and abs(oy) <= tolerance:
                fits = [fits[-1]]
                break
        builtin, chosen, cell, ox, oy = fits[0]
        if glyph is not None:
            glyph = chosen
        key = (cell.col, cell.row)
        if glyph is not None and (abs(ox) > TOLERANCE or abs(oy) > TOLERANCE):
            glyph, reason = None, f"{ox:+.1f},{oy:+.1f} units off its cell centre"
        inside = col <= key[0] < col + w and row <= key[1] < row + h
        if glyph is None:
            if inside:
                unknown.setdefault(key, []).append(obj)
                extras.append({"uid": obj.uid, "id": obj.type_id, "x": obj.x, "y": obj.y,
                               "rotation": obj.rotation, "reason": reason})
            continue
        if inside:
            anchors.setdefault(key, []).append((obj, glyph, builtin))
        for dc, dr in footprint(obj.role_entry.hitbox, glyph.rotation):
            covered.add((key[0] + dc, key[1] + dr))

    # Custom characters, assigned in reading order so a response is deterministic.
    custom: dict[Glyph, str] = {}
    free = iter(CUSTOM_CHARS)
    legend_used: dict[str, dict[str, Any]] = {}
    overlaps: list[dict[str, Any]] = []
    notes: list[str] = []
    out_of_chars = 0

    def char_for(glyph: Glyph, builtin: Builtin | None) -> str | None:
        if builtin is not None:
            return builtin.char
        if glyph not in custom:
            nxt = next(free, None)
            if nxt is None:
                return None
            custom[glyph] = nxt
        return custom[glyph]

    lines: list[str] = []
    for r in range(row + h - 1, row - 1, -1):
        chars: list[str] = []
        for c in range(col, col + w):
            key = (c, r)
            if key in unknown:
                chars.append(UNKNOWN)
                continue
            here = anchors.get(key)
            if here:
                here = sorted(here, key=lambda item: (_PRIORITY.get(item[0].role, 9),
                                                      item[0].uid))
                if len(here) > 1:
                    overlaps.append({"cell": [c, r], "uids": [o.uid for o, _, _ in here]})
                obj, glyph, builtin = here[0]
                char = char_for(glyph, builtin)
                if char is None:
                    out_of_chars += 1
                    chars.append(UNKNOWN)
                    extras.append({"uid": obj.uid, "id": obj.type_id, "x": obj.x, "y": obj.y,
                                   "rotation": obj.rotation,
                                   "reason": "no custom character left in this response"})
                    continue
                used = legend_used.setdefault(char, {**glyph.to_json(), "count": 0})
                used["count"] += 1
                chars.append(char)
            elif key in covered:
                chars.append(CONTINUATION)
            else:
                chars.append(EMPTY)
        lines.append("".join(chars))

    if out_of_chars:
        notes.append(f"{out_of_chars} objects shown as '?': more distinct objects than "
                     "custom characters; render a narrower range")
    hidden = sorted(set(LAYER_ROLES) - set(layers))
    for layer in hidden:
        count = len(model.in_rect(col * CELL, grid.ground_top_y + row * CELL,
                                  (col + w) * CELL, grid.ground_top_y + (row + h) * CELL,
                                  roles=LAYER_ROLES[layer]))
        if count:
            notes.append(f"{count} {layer} objects hidden (layers={','.join(layers)})")
    for char in sorted(_UNMEASURED_CHARS & set(legend_used)):
        notes.append(f"'{char}' is matched at the cell centre: where the editor puts it in "
                     "the cell is not measured yet (spike S-L9)")

    block = GridBlock(col, row, w, h, tuple(lines))
    return Rendered(block, legend_used, extras, overlaps, notes)
