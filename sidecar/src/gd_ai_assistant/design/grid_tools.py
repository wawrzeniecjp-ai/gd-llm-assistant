"""The GridText tools: `render_grid` and `place_grid` (LEVEL-DESIGN-SPEC.md 5.3, 5.4).

Pure functions over a level snapshot. The agent loop owns everything with side effects:
loading the snapshot, the per-turn object cap, the transaction, and sending the
`delete_objects` / `create_objects` requests a `PlaceGridPlan` describes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .coords import CELL, Grid
from .grid import (
    LAYER_ROLES,
    MAX_COLS,
    MAX_ROWS,
    GridError,
    emit,
    parse,
    parse_legend,
    plan_placement,
    render,
)
from .level_model import LevelModel
from .roles import GAMEPLAY_ROLES, RoleTable

#: Rows shown above the highest gameplay object when `y_range` is not given (5.3).
_HEADROOM_ROWS = 2

#: Most clashing cells listed in an `add` refusal; the rest are counted.
_MAX_CLASHES_LISTED = 20

#: `create_objects` accepts at most this many objects per request (protocol limit).
_MAX_CREATE = 5000


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"error": message, **extra}


def _span(lo: float, hi: float) -> tuple[int, int]:
    """Cells covering [lo, hi] in units: an edge exactly on a cell boundary is excluded."""
    lo, hi = min(lo, hi), max(lo, hi)
    first = math.floor(lo / CELL)
    last = max(first, math.ceil(hi / CELL) - 1)
    return first, last


def render_grid(model: LevelModel, grid: Grid, args: dict[str, Any]) -> dict[str, Any]:
    """`render_grid`: the level as GridText, with everything it could not show listed."""
    layers = args.get("layers") or ["gameplay"]
    unknown_layers = [layer for layer in layers if layer not in LAYER_ROLES]
    if unknown_layers:
        return _error(f"unknown layers {unknown_layers}; use {sorted(LAYER_ROLES)}")

    if args.get("cols") is not None:
        c0, c1 = sorted(int(v) for v in args["cols"])
    elif args.get("x_range") is not None:
        c0, c1 = _span(*(float(v) for v in args["x_range"]))
    else:
        return _error("give `x_range` (level units) or `cols` (grid columns)")
    width = c1 - c0 + 1
    if width > MAX_COLS:
        return _error(
            f"{width} columns is over the {MAX_COLS}-column limit; split the range into "
            f"{math.ceil(width / MAX_COLS)} calls"
        )

    if args.get("rows") is not None:
        r0, r1 = sorted(int(v) for v in args["rows"])
    elif args.get("y_range") is not None:
        lo, hi = (float(v) - grid.ground_top_y for v in args["y_range"])
        r0, r1 = _span(lo, hi)
    else:
        r0 = 0
        objects = model.in_rect(c0 * CELL, -1e9, (c1 + 1) * CELL, 1e9, roles=GAMEPLAY_ROLES)
        top = max((grid.row_of(o.y) for o in objects), default=0)
        r1 = min(max(top, 0) + _HEADROOM_ROWS, r0 + MAX_ROWS - 1)
    height = r1 - r0 + 1
    if height > MAX_ROWS:
        return _error(
            f"{height} rows is over the {MAX_ROWS}-row limit; narrow `y_range` or `rows`"
        )

    rendered = render(model, grid, col=c0, row=r0, w=width, h=height, layers=layers)
    result: dict[str, Any] = {
        "grid": emit(rendered.block),
        "legend_used": rendered.legend_used,
        "extras": rendered.extras,
        "overlaps": rendered.overlaps,
        "notes": rendered.notes,
        "revision": model.revision,
    }
    if rendered.custom_legend:
        # Ready to paste into place_grid's `legend`, so an unusual object survives a copy.
        result["legend_for_place_grid"] = rendered.custom_legend
    return result


@dataclass
class PlaceGridPlan:
    """What `place_grid` will do. `preview` plans are reported and not executed."""

    creates: list[dict[str, Any]]
    deletes: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    preview: bool = False
    verify: dict[str, Any] | None = None
    block_x: tuple[float, float] = (0.0, 0.0)

    @property
    def object_count(self) -> int:
        return len(self.creates) + len(self.deletes)

    def preview_result(self) -> dict[str, Any]:
        return {
            "preview": True,
            "would_create": len(self.creates),
            "would_delete": len(self.deletes),
            "objects": self.creates,
            "notes": self.notes,
        }


def plan_place_grid(
    model: LevelModel, grid: Grid, roles: RoleTable, args: dict[str, Any]
) -> PlaceGridPlan | dict[str, Any]:
    """`place_grid`, up to the point of touching the editor. Returns a plan or an error."""
    verify_spec = args.get("verify")
    if verify_spec is not None and not isinstance(verify_spec, dict):
        return _error("`verify` must be an object: {gamemode, speed, entry_row}")
    mode = args.get("mode", "add")
    if mode not in ("add", "replace"):
        return _error("`mode` must be 'add' or 'replace'")
    text = args.get("grid")
    if not isinstance(text, str):
        return _error("`grid` must be a GridText block (a string)")

    try:
        block = parse(text)
        legend = parse_legend(args.get("legend"))
        placement = plan_placement(block, legend, grid, roles)
    except GridError as exc:
        return _error("the grid has problems; nothing was placed", problems=exc.problems)

    if len(placement.payloads) > _MAX_CREATE:
        return _error(f"{len(placement.payloads)} objects is over the {_MAX_CREATE} per-call "
                      "limit; split the block")

    in_block = model.in_rect(
        block.col * CELL, grid.ground_top_y + block.row * CELL,
        (block.col + block.w) * CELL, grid.ground_top_y + (block.row + block.h) * CELL,
        roles=GAMEPLAY_ROLES,
    )
    # Centre-in-rectangle with inclusive edges would catch the next cell's objects too.
    in_block = [o for o in in_block if block.contains(grid.col_of(o.x), grid.row_of(o.y))]

    deletes: list[int] = []
    if mode == "add":
        targets = placement.cells
        clashes = sorted(
            {(grid.col_of(o.x), grid.row_of(o.y), o.uid) for o in in_block
             if (grid.col_of(o.x), grid.row_of(o.y)) in targets}
        )
        if clashes:
            listed = [{"cell": [c, r], "uid": uid} for c, r, uid in clashes[:_MAX_CLASHES_LISTED]]
            return _error(
                f"{len(clashes)} cells already hold gameplay objects; nothing was placed. "
                "Leave those cells empty in the grid, or use mode 'replace' to rebuild the "
                "whole block",
                clashes=listed,
            )
    else:
        deletes = [o.uid for o in in_block]

    return PlaceGridPlan(
        creates=placement.payloads,
        deletes=deletes,
        notes=list(placement.notes),
        preview=bool(args.get("preview", False)),
        verify=verify_spec,
        block_x=(block.col * CELL, (block.col + block.w) * CELL),
    )
