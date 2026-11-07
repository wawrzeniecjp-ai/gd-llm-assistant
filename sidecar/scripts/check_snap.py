"""Spike S-L9: where the editor's own grid placement puts objects inside their cell.

GridText places every object at its cell centre, but the editor does not: a half slab
sits at the bottom of its cell, a pad on the floor. This measures the offset per id, for
the built-in characters whose placement is not "centre" (LEVEL-DESIGN-SPEC.md 5.2.3).

Run from `sidecar/`, with `start-sidecar.bat` closed and a level open in the editor:

    uv run python scripts/check_snap.py

`build/snap-objects.png` shows each object (cut from the game's sprite sheets). For each
object it asks you to place one **by clicking in the editor's Build tab** (so
the editor snaps it as it normally would), select only it, and press Enter. The report
is written to `build/snap-report.json`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from _harness import ask, ok, run

from gd_ai_assistant.design.coords import Grid
from gd_ai_assistant.server import SidecarServer

REPORT = Path(__file__).resolve().parents[2] / "build" / (
    "snap-report.json" if len(sys.argv) == 1 else "snap-report-partial.json")

#: (id, what to place, the GridText character it backs)
OBJECTS: list[tuple[int, str, str]] = [
    (40, "a half slab: the half-height block, an outlined rectangle (id 40 or 1903)", "="),
    (39, "a small spike: the short, wide one", ","),
    (35, "a yellow jump pad", "_"),
    (140, "a pink jump pad", "~"),
    (67, "a blue gravity pad", "!"),
    (36, "a yellow jump orb (control: expected at the centre)", "o"),
]

#: Other ids accepted for an entry: objects that look the same in the editor's palette.
#: The first slab run placed 662 (a 15-unit outlined half block), which is worth
#: recording too, since it is a common slab.
ALTERNATES: dict[int, set[int]] = {40: {1903, 662, 147}}  # 147: the invisible plank, same hitbox

#: Run only some characters: `uv run python scripts/check_snap.py =`
ONLY: set[str] = set(sys.argv[1:])


async def routine(server: SidecarServer) -> dict[str, Any]:
    grid = Grid.from_constants()
    results = []
    print("\nFor each object: place ONE with a normal click in the Build tab, on the ground "
          "or on a block,\nselect only it, then press Enter here. Type 's' + Enter to skip.")
    for object_id, what, char in OBJECTS:
        if ONLY and char not in ONLY:
            continue
        answer = await ask(f"\n  Place {what} (id {object_id}, '{char}'). [Enter / s] ")
        if answer.strip().lower() == "s":
            continue
        picked = (await ok(server, "query_objects",
                           {"filters": {"selected_only": True}})).get("objects", [])
        accepted = {object_id} | ALTERNATES.get(object_id, set())
        if len(picked) != 1 or picked[0].get("id") not in accepted:
            ids = [p.get("id") for p in picked]
            print(f"    expected exactly one id {object_id} selected, got {ids}; skipped")
            results.append({"id": object_id, "char": char, "error": f"selected {ids}"})
            continue
        x, y = float(picked[0]["x"]), float(picked[0]["y"])
        dx, dy = grid.offset_from_centre(x, y)
        cell = grid.cell_of(x, y)
        print(f"    at ({x:g}, {y:g}): cell ({cell.col}, {cell.row}), "
              f"offset from centre dx={dx:+g} dy={dy:+g}")
        results.append({"id": picked[0]["id"], "char": char, "x": x, "y": y,
                        "dx": dx, "dy": dy, "cell": [cell.col, cell.row]})
    print("\nDone. You can delete the test objects.")
    return {"snap": results}


def main() -> None:
    report = run(routine)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport written to {REPORT}")


if __name__ == "__main__":
    main()
