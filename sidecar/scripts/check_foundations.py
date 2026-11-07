"""In-game checks for milestone M0 (LEVEL-DESIGN-SPEC.md 20, 21).

Covers, in one sitting:

1. **Mod version** -- the connected mod reports a level revision (the new build is loaded).
2. **S-L2 (quick)** -- off-scene description of a block and a spike, before the full dump.
3. **Coordinates** -- protocol `x`/`y` against save-string keys 2/3 for real objects. This
   settles whether the two disagree (docs/save-string-keys.md and
   src/editor/properties.hpp give opposite signs for the 90-unit offset).
4. **Snapshot timing** -- a full `get_level_objects` read of the open level (M0 target:
   10k objects in under 3 s).
5. **S-L10** -- does the revision move for each kind of edit? You make the edits; the
   script checks the counter after each one.

Run from `sidecar/`, with `start-sidecar.bat` closed and a level open in the editor (one
with a few hundred objects or more is best for the timing):

    uv run python scripts/check_foundations.py

The report is printed and also written to `build/foundations-report.json` in the repo.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from _harness import ask, ok, run

from gd_ai_assistant.design.level_model import load_level_model, parse_save_string
from gd_ai_assistant.design.roles import RoleTable
from gd_ai_assistant.server import SidecarServer

REPORT = Path(__file__).resolve().parents[2] / "build" / "foundations-report.json"

#: (instruction, whether the revision is expected to move)
HAND_EDITS: list[tuple[str, bool]] = [
    ("Do nothing (control).", False),
    ("Place one new block anywhere.", True),
    ("Select it and move it one step with an arrow key / the move buttons.", True),
    ("Rotate it with the editor's rotate button.", True),
    ("Open Edit Object on it and change its colour (or any property), then close.", True),
    ("Duplicate it (Ctrl+D or the copy+paste buttons).", True),
    ("Delete one of the two copies.", True),
    ("Undo (Ctrl+Z / the undo button).", True),
    ("Redo (the redo button).", True),
    ("Pan and zoom the view only -- change nothing.", False),
]


async def revision(server: SidecarServer) -> int | None:
    state = await ok(server, "get_editor_state")
    value = state.get("revision")
    return value if isinstance(value, int) else None


async def routine(server: SidecarServer) -> dict[str, Any]:
    report: dict[str, Any] = {}

    # 1. Mod version -----------------------------------------------------------
    start = await revision(server)
    report["mod_reports_revision"] = start is not None
    if start is None:
        print("\nThe connected mod does not report `revision`: it is an older build.")
        print("Rebuild and reinstall the mod (cmake --build build), restart the game, retry.")
        return report
    print(f"\n[1] mod reports revision {start} -- new build loaded")

    # 2. S-L2 quick ------------------------------------------------------------------
    described = await ok(server, "describe_object_types", {"ids": [1, 8]})
    report["describe_sample"] = described
    print("[2] describe_object_types for ids 1 and 8:")
    for entry in described.get("entries", []):
        print(f"    id {entry['id']}: type {entry['object_type']} hitbox {entry['hitbox']} "
              f"radius {entry['radius']} trigger={entry['is_trigger']} "
              f"deco={entry['is_decoration']}")
    if described.get("failed"):
        print(f"    refused: {described['failed']}")

    # 3. Coordinates -------------------------------------------------------------------
    page = await ok(server, "get_level_objects", {"limit": 200})
    dx: list[float] = []
    dy: list[float] = []
    for record in page.get("objects", []):
        raw = parse_save_string(record["save"])
        props = record["properties"]
        try:
            dx.append(round(float(props["x"]) - float(raw[2]), 3))
            dy.append(round(float(props["y"]) - float(raw[3]), 3))
        except (KeyError, ValueError):
            continue
    report["coordinates"] = {
        "samples": len(dy),
        "protocol_minus_save_x": sorted(set(dx)),
        "protocol_minus_save_y": sorted(set(dy)),
    }
    print(f"[3] protocol minus save-string, over {len(dy)} objects:")
    print(f"    x: {sorted(set(dx))[:5]}")
    print(f"    y: {sorted(set(dy))[:5]}")
    if dy and set(dy) == {0.0}:
        print("    -> protocol y IS the save-string y.")
    elif dy:
        print("    -> they differ: record this in docs/save-string-keys.md before M1.")
    else:
        print("    -> no objects in this level; place a few and run again for this check.")

    # 4. Snapshot timing -------------------------------------------------------------------
    began = time.perf_counter()
    model = await load_level_model(server, RoleTable(), page_size=2000)
    elapsed = time.perf_counter() - began
    report["snapshot"] = {"objects": len(model), "seconds": round(elapsed, 3),
                          "revision": model.revision}
    rate = len(model) / elapsed if elapsed > 0 else 0
    print(f"[4] snapshot: {len(model)} objects in {elapsed:.2f}s ({rate:.0f}/s)")

    # 5. S-L10 -----------------------------------------------------------------------------
    print("\n[5] Revision check. Do each step in the editor, then press Enter here.")
    results: list[dict[str, Any]] = []
    for instruction, expected in HAND_EDITS:
        before = await revision(server)
        await ask(f"    {instruction}  [Enter when done] ")
        after = await revision(server)
        moved = before != after
        verdict = "ok" if moved == expected else "UNEXPECTED"
        results.append({"step": instruction, "before": before, "after": after,
                        "moved": moved, "expected": expected})
        print(f"      revision {before} -> {after}: {'moved' if moved else 'unchanged'} "
              f"({verdict})")

    # The assistant's own edits, made by this script through a transaction.
    before = await revision(server)
    txn = (await ok(server, "begin_transaction", {"label": "foundations check"}))["txn_id"]
    created = await ok(server, "create_objects", {"objects": [{"id": 1, "x": 15, "y": 15}]})
    mid1 = await revision(server)
    uid = created["created"][0]
    await ok(server, "modify_objects", {"targets": [uid], "delta": {"x": 30}})
    mid2 = await revision(server)
    await ok(server, "delete_objects", {"targets": [uid]})
    after = await revision(server)
    await ok(server, "end_transaction", {"txn_id": txn})
    sequence = [before, mid1, mid2, after]
    moved_each = all(a != b for a, b in zip(sequence, sequence[1:], strict=False))
    results.append({"step": "assistant create/modify/delete", "sequence": sequence,
                    "moved": moved_each, "expected": True})
    print(f"    assistant create -> modify -> delete: {sequence} "
          f"({'ok' if moved_each else 'UNEXPECTED'})")
    report["revision_checks"] = results

    unexpected = [r["step"] for r in results if r["moved"] != r["expected"]]
    report["revision_unexpected"] = unexpected
    print("\nS-L10:", "all as expected" if not unexpected else f"UNEXPECTED: {unexpected}")

    if len(model) and elapsed:
        report["snapshot"]["per_10k_seconds"] = round(elapsed * 10000 / len(model), 2)
    if dy:
        report["coordinates"]["median_dy"] = statistics.median(dy)
    return report


def main() -> None:
    report = run(routine)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport written to {REPORT}")


if __name__ == "__main__":
    main()
