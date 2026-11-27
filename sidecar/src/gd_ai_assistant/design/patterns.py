"""The pattern library (LEVEL-DESIGN-SPEC.md 10): reusable, verified gameplay pieces.

A pattern is a relative GridText block (`col=0 row=0`) with the player state it expects
and leaves. It is saved only if the simulator plays it: `verify_pattern` builds it alone,
after a few empty columns of run-up, and checks it from its entry state with
`verify_section`'s explicit start. Seed patterns are committed under
`data/patterns/seed/`; user patterns, extracted from the user's own levels, under the
data directory's `patterns/`.

Two variants make one authored pattern serve more places:

- `flip_gravity` mirrors it upside down (`^` and `v` swap, as do `=` and `-` and the two
  gravity portals). A pattern with a floor gets a ceiling row of it on top.
- `rescale_speed` re-spaces it for another speed. Each run of identical cells keeps its
  length and only where runs start scales with the speed's units per second, so a
  triple spike stays a triple spike and the gaps grow or shrink. (The spec's literal
  rule scales every column, which at 0.5x would shrink a triple spike to two and merge
  block runs.) The result is verified again; a pattern that no longer plays is refused.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from .constants import SPEEDS, PhysicsConstants, load_constants
from .constants import _default_path as constants_path
from .coords import CELL, Grid
from .grid import (
    CONTINUATION,
    EMPTY,
    GridBlock,
    GridError,
    emit,
    parse,
    parse_legend,
    plan_placement,
)
from .roles import RoleTable, load_role_table

SCHEMA = 1
#: Empty columns before a pattern when it is verified alone: a run-up, so the player is
#: moving at speed and an input in the first column is possible.
LEAD_COLS = 6
#: And after it, so the exit is measured past the last object.
TAIL_COLS = 3
#: Modes a pattern may declare.
PATTERN_MODES = ("cube", "ship", "ball", "ufo", "wave", "robot", "spider", "swing")
#: Characters `flip_gravity` maps to their mirror image; the others stay as they are.
_FLIP = {"^": "v", "v": "^", "=": "-", "-": "=", "+": "&", "&": "+"}
#: Characters with no mirror image in the built-in legend (pads and the small spike sit on
#: the floor of their cell): a pattern holding one cannot be flipped.
_UNFLIPPABLE = frozenset("_~!,")
_ID = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
#: An upside-down pattern is verified this many rows up, clear of the level's ground: its
#: jumps go down, and the ground would catch them (it plays in the open in a level).
FLIP_LIFT = 10
#: Modes `flip_gravity` applies to: the ground modes without a band. A flight mode's
#: band is not mirrored by flipping the pattern.
FLIPPABLE_MODES = ("cube", "robot")


class PatternError(ValueError):
    """A pattern is malformed, or a variant of it cannot be made."""


@dataclass(frozen=True)
class Pattern:
    id: str
    name: str
    tags: tuple[str, ...]
    mode: str
    speed: str
    gravity: int
    mini: bool
    difficulty: int
    grid: str
    legend: Mapping[str, Any] = field(default_factory=dict)
    floor: str | None = "#"
    entry: Mapping[str, Any] = field(default_factory=lambda: {"row": 0, "grounded": True,
                                                               "vy": 0})
    exit: Mapping[str, Any] = field(default_factory=lambda: {"row": 0, "grounded": True})
    verified: Mapping[str, Any] | None = None
    source: Mapping[str, Any] = field(default_factory=lambda: {"kind": "seed"})

    @property
    def block(self) -> GridBlock:
        return parse(self.grid)

    @property
    def width(self) -> int:
        return self.block.w

    @property
    def height(self) -> int:
        return self.block.h

    @property
    def exit_mode(self) -> str:
        return str(self.exit.get("mode", self.mode))

    @property
    def exit_gravity(self) -> int:
        return int(self.exit.get("gravity", self.gravity))

    @classmethod
    def from_json(cls, raw: Mapping[str, Any], *, where: str = "pattern") -> Pattern:
        problems: list[str] = []
        if raw.get("schema") != SCHEMA:
            problems.append(f"schema must be {SCHEMA}")
        pid = str(raw.get("id", ""))
        if not _ID.match(pid):
            problems.append(f"id {pid!r} must be lowercase letters, digits, '.' and '-'")
        mode = raw.get("mode")
        if mode not in PATTERN_MODES:
            problems.append(f"mode {mode!r} is not one of {', '.join(PATTERN_MODES)}")
        speed = raw.get("speed")
        if speed not in SPEEDS:
            problems.append(f"speed {speed!r} is not one of {', '.join(SPEEDS)}")
        if raw.get("gravity", 1) not in (1, -1):
            problems.append("gravity must be 1 or -1")
        difficulty = raw.get("difficulty")
        if not isinstance(difficulty, int) or not 1 <= difficulty <= 10:
            problems.append("difficulty must be an integer from 1 to 10")
        try:
            block = parse(str(raw.get("grid", "")))
            if (block.col, block.row) != (0, 0):
                problems.append("grid must be relative: '@grid col=0 row=0 ...'")
            parse_legend(raw.get("legend") or {})
        except GridError as exc:
            problems.extend(f"grid: {p}" for p in exc.problems)
        if problems:
            raise PatternError(f"{where}: " + "; ".join(problems))
        return cls(id=pid, name=str(raw.get("name") or pid), tags=tuple(raw.get("tags") or ()),
                   mode=str(mode), speed=str(speed), gravity=int(raw.get("gravity", 1)),
                   mini=bool(raw.get("mini", False)), difficulty=int(difficulty or 1),
                   grid=str(raw["grid"]), legend=dict(raw.get("legend") or {}),
                   floor=raw.get("floor", "#"),
                   entry=dict(raw.get("entry") or {"row": 0, "grounded": True, "vy": 0}),
                   exit=dict(raw.get("exit") or {"row": 0, "grounded": True}),
                   verified=raw.get("verified"),
                   source=dict(raw.get("source") or {"kind": "seed"}))

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA, "id": self.id, "name": self.name, "tags": list(self.tags),
            "mode": self.mode, "speed": self.speed, "gravity": self.gravity,
            "mini": self.mini, "difficulty": self.difficulty, "grid": self.grid,
            "legend": dict(self.legend), "floor": self.floor, "entry": dict(self.entry),
            "exit": dict(self.exit),
            **({"verified": dict(self.verified)} if self.verified is not None else {}),
            "source": dict(self.source),
        }


def base_id(pattern: Pattern) -> str:
    """The id without its speed suffix (`cube.triple-spike.0.5x` -> `cube.triple-spike`;
    `0.5x` holds a dot, so splitting on the last one is wrong)."""
    suffix = f".{pattern.speed}"
    return pattern.id[: -len(suffix)] if pattern.id.endswith(suffix) else pattern.id


# -- the library ---------------------------------------------------------------------------


def seed_dir() -> Path:
    return Path(str(resources.files("gd_ai_assistant") / "data" / "patterns" / "seed"))


def user_dir() -> Path:
    """`<data_dir>/patterns/`, beside the token file (SPEC.md 7.2)."""
    import os

    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return base / "gd-ai-assistant" / "patterns"


def load_dir(folder: Path, kind: str) -> list[Pattern]:
    out = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        pattern = Pattern.from_json(json.loads(path.read_text(encoding="utf-8")),
                                    where=str(path.name))
        if pattern.source.get("kind") != kind:
            pattern = replace(pattern, source={**pattern.source, "kind": kind})
        out.append(pattern)
    return out


@lru_cache(maxsize=1)
def load_seed() -> tuple[Pattern, ...]:
    return tuple(load_dir(seed_dir(), "seed"))


def load_library(user: Path | None = None) -> list[Pattern]:
    """Seed patterns, then the user's (a user pattern with a seed's id is skipped)."""
    seeds = list(load_seed())
    ids = {p.id for p in seeds}
    return seeds + [p for p in load_dir(user or user_dir(), "user") if p.id not in ids]


def save_pattern(pattern: Pattern, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{pattern.id}.json"
    path.write_text(json.dumps(pattern.to_json(), indent=1) + "\n", encoding="utf-8")
    return path


def constants_version(path: Path | None = None) -> str:
    """A short hash of the constants file a pattern was verified with (10.3)."""
    data = (path or constants_path()).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()[:12]


# -- verification ---------------------------------------------------------------------------


def shifted(block: GridBlock, col: int, row: int = 0) -> GridBlock:
    return replace(block, col=col, row=row)


def pattern_objects(pattern: Pattern, grid: Grid, roles: RoleTable, *, col: int = LEAD_COLS,
                    row: int = 0) -> list[dict[str, Any]]:
    """The create_objects payloads of the pattern with its corner at (col, row)."""
    placement = plan_placement(shifted(pattern.block, col, row),
                               parse_legend(pattern.legend), grid, roles)
    return placement.payloads


def base_row(pattern: Pattern) -> int:
    """The row a pattern is verified at: 0, or `FLIP_LIFT` upside down."""
    return FLIP_LIFT if pattern.gravity < 0 else 0


def _start(pattern: Pattern) -> dict[str, Any]:
    entry = pattern.entry
    return {"mode": pattern.mode, "speed": pattern.speed, "gravity": pattern.gravity,
            "mini": pattern.mini, "row": int(entry.get("row", 0)) + base_row(pattern),
            "vy": float(entry.get("vy", 0.0)),
            "band_floor": float(entry.get("band_floor", 0.0))}


def verify_pattern(pattern: Pattern, *, constants: PhysicsConstants | None = None,
                   roles: RoleTable | None = None,
                   budget_s: float = 10.0) -> dict[str, Any]:
    """The `verified` record for a pattern: `result` (`possible` or why not), its
    difficulty, its exit state, and the constants version. `possible` also needs the
    exit to match what the pattern declares (mode, gravity, and the row when grounded)."""
    from .sim.cube import CubeParams, half_of
    from .sim.verify import verify
    from .sim.world import world_from_objects

    constants = constants or load_constants()
    roles = roles or load_role_table()
    grid = Grid.from_constants(constants)
    params = CubeParams.from_constants(constants)
    lift = base_row(pattern)
    objects = pattern_objects(pattern, grid, roles, row=lift)
    if pattern.gravity < 0 and pattern.floor:
        # Upside down, its floor is the ceiling on top: it runs through the run-up and
        # tail as the level's ground does for an upright pattern.
        top = lift + pattern.height - 1
        run = [*range(LEAD_COLS), *range(LEAD_COLS + pattern.width,
                                         LEAD_COLS + pattern.width + TAIL_COLS)]
        ceiling = GridBlock(0, 0, 1, 1, (pattern.floor,))
        legend = parse_legend(pattern.legend)
        for col in run:
            objects += plan_placement(shifted(ceiling, col, top), legend, grid,
                                      roles).payloads
    world = world_from_objects(objects, roles)
    x1 = (LEAD_COLS + pattern.width + TAIL_COLS) * CELL
    result = verify(world, params, {"x_range": [0.0, x1], "start_from": "explicit",
                                    "start": _start(pattern), "budget_s": budget_s,
                                    "difficulty": pattern.difficulty})
    out: dict[str, Any] = {"result": result["result"],
                           "constants_version": constants_version()}
    if result["result"] != "possible":
        reason = (result.get("reason") or (result.get("unverifiable") or [{}])[0].get("reason")
                  or (f"dies by x={result.get('furthest_x')}" if result.get("furthest_x")
                      is not None else None))
        if reason:
            out["reason"] = reason
        return out
    final = result["exit"]
    route = result.get("route", [])
    out.update(min_window_ms=result["difficulty"]["min_window_ms"],
               inputs=len(route),
               holds=sum(1 for r in route if r.get("action") == "hold"),
               exit={"mode": final["mode"], "gravity": final["gravity"],
                     "grounded": final["grounded"], "y": final["y"]})
    if "in_band" in result:
        out["in_band"] = result["in_band"]
    problems = []
    if final["mode"] != pattern.exit_mode:
        problems.append(f"exits as a {final['mode']}, declares {pattern.exit_mode}")
    if final["gravity"] != pattern.exit_gravity:
        problems.append(f"exits with gravity {final['gravity']}, declares "
                        f"{pattern.exit_gravity}")
    if pattern.exit.get("grounded"):
        from .sim.cube import Cube
        half = half_of(Cube(mode=final["mode"], mini=pattern.mini), params)
        row = (round((final["y"] - half) / CELL) if final["gravity"] > 0
               else round((final["y"] + half) / CELL) - 1) - lift
        if not final["grounded"]:
            problems.append("exits in the air, declares grounded")
        elif row != int(pattern.exit.get("row", 0)):
            problems.append(f"exits on row {row}, declares {pattern.exit.get('row', 0)}")
    if problems:
        out["result"] = "exit_mismatch"
        out["reason"] = "; ".join(problems)
    return out


def verified(pattern: Pattern, **kwargs: Any) -> Pattern:
    """The pattern with a fresh `verified` record."""
    return replace(pattern, verified=verify_pattern(pattern, **kwargs))


# -- variants -------------------------------------------------------------------------------


def _with_rows(pattern: Pattern, rows: list[str], width: int, **changes: Any) -> Pattern:
    block = GridBlock(0, 0, width, len(rows), tuple(rows))
    return replace(pattern, grid=emit(block), verified=None, **changes)


def flip_gravity(pattern: Pattern) -> Pattern:
    """The pattern upside down: rows mirrored, orientations swapped, gravity flipped, and
    a ceiling of its floor character on top when it has one."""
    if pattern.mode not in FLIPPABLE_MODES:
        raise PatternError(f"{pattern.id}: flip_gravity is for {' and '.join(FLIPPABLE_MODES)}"
                           " patterns; a flight mode's band is not mirrored")
    block = pattern.block
    bad = sorted({c for row in block.rows for c in row} & _UNFLIPPABLE)
    if bad:
        raise PatternError(f"{pattern.id} cannot be flipped: {''.join(bad)!r} has no "
                           "upside-down character")
    rows = ["".join(_FLIP.get(c, c) for c in row) for row in reversed(block.rows)]
    height = len(rows)
    if pattern.floor:
        rows = [pattern.floor * block.w, *rows]

    def mirrored(state: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(state)
        if "row" in out:
            out["row"] = height - 1 - int(out["row"])
        if "vy" in out:
            out["vy"] = -float(out["vy"]) or 0.0
        if "gravity" in out:
            out["gravity"] = -int(out["gravity"])
        return out

    new_id = f"{base_id(pattern)}.flip.{pattern.speed}"
    return _with_rows(pattern, rows, block.w, id=new_id, gravity=-pattern.gravity,
                      entry=mirrored(pattern.entry), exit=mirrored(pattern.exit),
                      tags=tuple(dict.fromkeys((*pattern.tags, "upside-down"))))


def rescale_speed(pattern: Pattern, speed: str,
                  constants: PhysicsConstants | None = None) -> Pattern:
    """The pattern re-spaced for `speed` (not verified: see `rescaled`)."""
    if speed not in SPEEDS:
        raise PatternError(f"unknown speed {speed!r}")
    if speed == pattern.speed:
        return pattern
    constants = constants or load_constants()
    ups = constants.units_per_second
    old, new = ups[pattern.speed].value, ups[speed].value
    if not old or not new:
        raise PatternError("the speeds' units per second are not calibrated")
    ratio = new / old
    block = pattern.block
    if any(CONTINUATION in row for row in block.rows):
        raise PatternError(f"{pattern.id} has multi-cell objects (':'); it cannot be "
                           "re-spaced")
    width = max(1, round(block.w * ratio))
    cells: dict[tuple[int, int], str] = {}
    for index, line in enumerate(block.rows):
        col = 0
        while col < len(line):
            char = line[col]
            end = col
            while end + 1 < len(line) and line[end + 1] == char:
                end += 1
            if char != EMPTY:
                start = round(col * ratio)
                for k in range(end - col + 1):
                    key = (start + k, index)
                    if key in cells:
                        raise PatternError(f"{pattern.id} at {speed}: two features meet at "
                                           f"column {start + k}")
                    cells[key] = char
                width = max(width, start + end - col + 1)
            col = end + 1
    rows = ["".join(cells.get((c, r), EMPTY) for c in range(width))
            for r in range(len(block.rows))]
    return _with_rows(pattern, rows, width, id=f"{base_id(pattern)}.{speed}", speed=speed)


def rescaled(pattern: Pattern, speed: str, **kwargs: Any) -> Pattern:
    """`rescale_speed`, verified; raises `PatternError` if it does not play."""
    out = verified(rescale_speed(pattern, speed), **kwargs)
    assert out.verified is not None
    if out.verified["result"] != "possible":
        raise PatternError(f"{out.id} does not play at {speed}: "
                           f"{out.verified.get('reason') or out.verified['result']}")
    return out


def flipped(pattern: Pattern, **kwargs: Any) -> Pattern:
    """`flip_gravity`, verified; raises `PatternError` if it does not play."""
    out = verified(flip_gravity(pattern), **kwargs)
    assert out.verified is not None
    if out.verified["result"] != "possible":
        raise PatternError(f"{out.id} does not play upside down: "
                           f"{out.verified.get('reason') or out.verified['result']}")
    return out


# -- search --------------------------------------------------------------------------------


def search(patterns: Iterable[Pattern], *, query: str | None = None, mode: str | None = None,
           speed: str | None = None, difficulty: tuple[int, int] | None = None,
           tags: Iterable[str] = (), limit: int = 10) -> list[Pattern]:
    """Ranked: query words matched in tags, then whole words of the id and name, then
    parts of words; ties go to the easier and simpler."""
    words = [w for w in re.split(r"[\s,]+", (query or "").lower()) if w]
    wanted = {t.lower() for t in tags}
    scored = []
    for p in patterns:
        if mode and p.mode != mode and p.exit_mode != mode:
            continue
        if speed and p.speed != speed:
            continue
        if difficulty and not difficulty[0] <= p.difficulty <= difficulty[1]:
            continue
        ptags = {t.lower() for t in p.tags}
        if wanted and not wanted <= ptags:
            continue
        text = f"{p.id} {p.name}".lower()
        tokens = set(re.split(r"[^a-z0-9]+", text))
        # A tag counts most, a whole word of the id or name next, part of a word least
        # ("triple" is in "triplet": the triple spike must outrank the triplet rhythm).
        score = sum(3 if w in ptags else 2 if w in tokens else 1 if w in text else 0
                    for w in words)
        if words and score == 0:
            continue
        scored.append((-score, p.difficulty, p.width, p.id, p))
    scored.sort(key=lambda s: s[:4])
    return [s[-1] for s in scored[:limit]]
