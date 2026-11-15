"""Writes the evaluation harness's level fixtures (evals/fixtures/levels, spec 15.3).

    uv run python scripts/make_eval_fixtures.py

Each fixture is built here, so it can be read and rebuilt: synthetic layouts drawn as
GridText, M2 test sections moved to the level start (their start position dropped, so
the level start is where play begins), and the hand-built tricky columns from the solver
tests (built and beaten by the user, 2026-11-08).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from build_sections import payloads
from m2_sections import SECTIONS

from gd_ai_assistant.design.coords import Grid
from gd_ai_assistant.design.grid import parse, plan_placement
from gd_ai_assistant.design.roles import load_role_table

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "fixtures" / "levels"
START_POS = 31


def _grid(text: str) -> list[dict[str, Any]]:
    placed = plan_placement(parse(text), {}, Grid.from_constants(), load_role_table())
    return [dict(p) for p in placed.payloads]


def _slot(slot: int) -> list[dict[str, Any]]:
    section = next(s for s in SECTIONS if s.slot == slot)
    shift = section.col * 30
    return [{**p, "x": p["x"] - shift} for p in payloads(section, Grid.from_constants())
            if p["id"] != START_POS]


def _flat(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Payloads as `FakeEditor.add` takes them: id, x, y and named properties."""
    out = []
    for obj in objects:
        flat = {"id": obj["id"], "x": obj["x"], "y": obj["y"]}
        flat.update(obj.get("properties") or {})
        out.append(flat)
    return out


FIXTURES: dict[str, tuple[str, list[dict[str, Any]]]] = {
    "empty": ("An empty level.", []),
    "wall-1x": (
        "1x: a few spikes, then a wall five blocks high at column 30 that no jump clears.",
        _grid("@grid col=10 row=0 w=25 h=5\n"
              + "....................#....\n" * 4
              + "......^.......^^....#....")),
    "pattern-1x": (
        "1x: a 20-column unit three times, columns 10-69: a spike, a 2-wide block, two spikes.",
        _grid("@grid col=10 row=0 w=60 h=1\n" + "...^....##....^^...." * 3)),
    "slot2": ("M2 slot 2, possible: a 2-block step up, then down.", _slot(2)),
    "slot12": ("M2 slot 12, impossible: a 3-block step.", _slot(12)),
    "tricky-columns": (
        "0.5x, built and beaten by hand: possible, but each column needs a one-tick press.",
        json.loads((ROOT / "tests" / "fixtures" / "tricky_columns.json")
                   .read_text(encoding="utf-8"))["objects"]),
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (description, objects) in FIXTURES.items():
        data = {"description": description, "settings": {}, "colors": {},
                "objects": _flat(objects)}
        (OUT / f"{name}.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        print(f"{name}: {len(objects)} objects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
