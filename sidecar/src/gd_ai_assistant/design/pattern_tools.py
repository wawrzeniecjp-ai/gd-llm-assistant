"""The pattern tools (LEVEL-DESIGN-SPEC.md 10.4): `search_patterns`, `place_pattern` and
`extract_pattern`. Pure functions of the library and the level snapshot; the agent loop
sends `place_pattern`'s result through `place_grid`, so a pattern is placed and verified
exactly as a grid the model wrote would be."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import patterns as P
from .coords import CELL, Grid
from .grid import emit, parse
from .level_model import LevelModel
from .roles import RoleTable

#: Most patterns listed by one search.
MAX_RESULTS = 20


def _summary(p: P.Pattern, *, preview: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": p.id, "name": p.name, "mode": p.mode, "speed": p.speed,
        "size": [p.width, p.height], "difficulty": p.difficulty, "tags": list(p.tags),
        "min_window_ms": (p.verified or {}).get("min_window_ms"),
        "entry": dict(p.entry), "exit": {"mode": p.exit_mode, **dict(p.exit)},
        "source": p.source.get("kind", "seed"),
    }
    if preview:
        out["grid"] = p.grid
    return out


def search_patterns(args: dict[str, Any], library: list[P.Pattern] | None = None
                    ) -> dict[str, Any]:
    """`search_patterns { query?, mode?, speed?, difficulty?: [min,max], tags?, limit? }`."""
    library = library if library is not None else P.load_library()
    difficulty = args.get("difficulty")
    if difficulty is not None:
        if not (isinstance(difficulty, list) and len(difficulty) == 2):
            return {"error": "`difficulty` must be [min, max], 1-10"}
        difficulty = (int(min(difficulty)), int(max(difficulty)))
    limit = max(1, min(int(args.get("limit", 8)), MAX_RESULTS))
    found = P.search(library, query=args.get("query"), mode=args.get("mode"),
                     speed=args.get("speed"), difficulty=difficulty,
                     tags=args.get("tags") or (), limit=limit)
    out: dict[str, Any] = {"patterns": [_summary(p) for p in found],
                           "library_size": len(library)}
    if not found:
        out["note"] = ("Nothing matched. Try fewer words or tags, another speed, or "
                       "write the gameplay with place_grid.")
    return out


_RESCALE = re.compile(r"^rescale_speed:(\S+)$")


def resolve_variant(pattern: P.Pattern, variant: str) -> P.Pattern:
    """`as_is`, `flip_gravity`, or `rescale_speed:<speed>`; a variant that does not play
    raises `PatternError` (nothing is placed)."""
    if variant in ("", "as_is"):
        return pattern
    if variant == "flip_gravity":
        return P.flipped(pattern)
    match = _RESCALE.match(variant)
    if match:
        speed = match.group(1)
        # A committed speed is the verified one; rescale the 1x pattern otherwise.
        return P.rescaled(pattern, speed)
    raise P.PatternError(f"unknown variant {variant!r}: use as_is, flip_gravity or "
                         "rescale_speed:<speed>")


def prepare_place(args: dict[str, Any], library: list[P.Pattern] | None = None
                  ) -> dict[str, Any]:
    """`place_pattern { id, col, row?, variant? }` as `place_grid` arguments, with
    `verify` filled in from the pattern's entry state. Returns `{"error": ...}` instead
    when the pattern or its variant cannot be placed."""
    library = library if library is not None else P.load_library()
    by_id = {p.id: p for p in library}
    pid = args.get("id")
    pattern = by_id.get(str(pid))
    if pattern is None:
        return {"error": f"no pattern {pid!r}; use search_patterns to find one"}
    if "col" not in args:
        return {"error": "give `col`, the grid column of the pattern's left edge"}
    variant = str(args.get("variant") or "as_is")
    try:
        chosen = resolve_variant(pattern, variant)
    except P.PatternError as exc:
        return {"error": f"{exc}; nothing was placed"}
    col, row = int(args["col"]), int(args.get("row", 0))
    block = replace(parse(chosen.grid), col=col, row=row)
    entry = chosen.entry
    verify = {"gamemode": chosen.mode, "speed": chosen.speed,
              "entry_row": row + int(entry.get("row", 0)), "gravity": chosen.gravity,
              "mini": chosen.mini}
    if "band_floor" in entry:
        verify["band_floor"] = float(entry["band_floor"]) + row * CELL
    return {
        "grid": emit(block), "legend": dict(chosen.legend), "mode": "add",
        "verify": verify, "preview": bool(args.get("preview", False)),
        "_pattern": _summary(chosen, preview=False) | {"col": col, "row": row},
    }


def extract_pattern(model: LevelModel, grid: Grid, roles: RoleTable, args: dict[str, Any],
                    *, start_speed: str = "1x", folder: Path | None = None) -> dict[str, Any]:
    """`extract_pattern { x_range, y_range?, name, tags, difficulty? }`: a region of the
    user's level saved to their library, if GridText shows all of it and the simulator
    plays it alone from the state the level enters it in."""
    from .constants import load_constants
    from .grid_tools import render_grid
    from .sim.cube import Cube, CubeParams, half_of
    from .sim.verify import verify_section

    name = str(args.get("name") or "").strip()
    if not name:
        return {"error": "give the pattern a `name`"}
    x_range = args.get("x_range")
    if not x_range or len(x_range) != 2:
        return {"error": "give `x_range` as [start, end] in level units"}
    x0, x1 = sorted(float(v) for v in x_range)
    rendered = render_grid(model, grid, {"x_range": [x0, x1 - 1e-6],
                                         **({"y_range": args["y_range"]}
                                            if args.get("y_range") else {})})
    if "error" in rendered:
        return rendered
    if rendered["extras"] or rendered["overlaps"]:
        return {"error": "the region holds objects GridText cannot show exactly, so it "
                         "cannot be saved as a pattern",
                "extras": rendered["extras"][:10], "overlaps": rendered["overlaps"][:10]}
    block = parse(rendered["grid"])
    # The state the level enters the region in: verified from the level start.
    approach = verify_section(model, {"x_range": [0.0, block.col * CELL],
                                      "start_from": "level_start",
                                      "_level_start_speed": start_speed})
    if approach.get("result") != "possible":
        return {"error": "the level up to this region does not verify, so the state the "
                         "player enters it in is unknown",
                "approach": {k: approach.get(k) for k in ("result", "reason")}}
    state = approach["exit"]
    params = CubeParams.from_constants(load_constants())
    half = half_of(Cube(mode=state["mode"], mini=state["mini"]), params)
    abs_row = (round((state["y"] - half - grid.ground_top_y) / CELL) if state["gravity"] > 0
               else round((state["y"] + half - grid.ground_top_y) / CELL) - 1)
    entry: dict[str, Any] = {"row": abs_row - block.row, "grounded": state["grounded"],
                             "vy": state["vy"]}
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "pattern"
    pattern = P.Pattern(
        id=f"user.{slug}.{state['speed']}", name=name,
        tags=tuple(str(t) for t in (args.get("tags") or ())), mode=state["mode"],
        speed=state["speed"], gravity=state["gravity"], mini=state["mini"],
        difficulty=int(args.get("difficulty") or 1),
        grid=emit(replace(block, col=0, row=0)),
        legend=dict(rendered.get("legend_for_place_grid") or {}),
        floor="#" if block.row == 0 else None, entry=entry, exit={},
        source={"kind": "user", "from": {"x_range": [x0, x1], "row": block.row}})
    check = P.verify_pattern(pattern)
    if check["result"] not in ("possible", "exit_mismatch") or "exit" not in check:
        return {"error": f"it does not play alone from the state the level enters it in "
                         f"({check['result']}: {check.get('reason', '')}); not saved"}
    final = check["exit"]
    exit_state: dict[str, Any] = {"mode": final["mode"], "gravity": final["gravity"],
                                  "grounded": final["grounded"]}
    if final["grounded"]:
        out_half = half_of(Cube(mode=final["mode"], mini=pattern.mini), params)
        exit_state["row"] = (round((final["y"] - out_half) / CELL) if final["gravity"] > 0
                             else round((final["y"] + out_half) / CELL) - 1)
    pattern = P.verified(replace(pattern, exit=exit_state))
    assert pattern.verified is not None
    if pattern.verified["result"] != "possible":
        return {"error": f"it does not play alone: {pattern.verified.get('reason')}; "
                         "not saved"}
    path = P.save_pattern(pattern, folder or P.user_dir())
    return {"saved": True, "pattern": _summary(pattern), "path": str(path)}
