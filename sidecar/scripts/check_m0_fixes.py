"""Second M0 sitting: confirm the fixes from the first foundations check, and measure S-L1.

The first run of `check_foundations.py` found three problems, all fixed in the mod:

1. `describe_object_types` reported a spike as a solid 30x30 block (the off-scene object
   was never set up). Now it calls `customSetup`.
2. Protocol `y` was save-string `y` + 180 (the conversion had its sign flipped). Now the
   protocol `y` *is* the save-string `y`, and the camera speaks the same coordinates.
3. Edit Object changes did not move the level revision. Now the selection is hashed.

This script checks each fix, then measures the ground height (S-L1) from a block you
place on the ground row.

Run from `sidecar/`, with `start-sidecar.bat` closed, the game restarted (so the new mod
is loaded) and a level open in the editor:

    uv run python scripts/check_m0_fixes.py

The report is printed and written to `build/m0-fixes-report.json` in the repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _harness import ask, ok, run

from gd_ai_assistant.design.level_model import parse_save_string
from gd_ai_assistant.server import SidecarServer

REPORT = Path(__file__).resolve().parents[2] / "build" / "m0-fixes-report.json"

#: id -> what it is and the game type it should report (GameObjectType).
DESCRIBE: dict[int, tuple[str, int | None]] = {
    1: ("default block", 0),
    8: ("spike", 2),
    39: ("small spike", 2),
    36: ("yellow orb", 11),
    35: ("yellow pad", 8),
    12: ("cube portal", 6),
    13: ("ship portal", 5),
    10: ("blue gravity portal", None),
    88: ("saw", None),
}


async def revision(server: SidecarServer) -> int:
    return int((await ok(server, "get_editor_state"))["revision"])


async def selected(server: SidecarServer) -> list[dict[str, Any]]:
    result = await ok(server, "query_objects", {"filters": {"selected_only": True}})
    return list(result.get("objects", []))


async def routine(server: SidecarServer) -> dict[str, Any]:
    report: dict[str, Any] = {}

    # 1. describe_object_types -------------------------------------------------------
    described = await ok(server, "describe_object_types", {"ids": list(DESCRIBE)})
    rows = []
    print("\n[1] describe_object_types after the customSetup fix:")
    for entry in described.get("entries", []):
        name, expected = DESCRIBE.get(entry["id"], ("?", None))
        verdict = "" if expected is None else (
            "ok" if entry["object_type"] == expected else f"UNEXPECTED, wanted {expected}")
        hb = entry["hitbox"]
        print(f"    {entry['id']:>4} {name:<20} type {entry['object_type']:>2}  "
              f"hitbox {hb['w']:g}x{hb['h']:g} at ({hb['x']:g},{hb['y']:g})  "
              f"radius {entry['radius']:g}  {verdict}")
        rows.append({**entry, "name": name, "expected_type": expected})
    report["describe"] = rows
    report["describe_failed"] = described.get("failed", [])
    report["describe_unexpected"] = [
        r["id"] for r in rows
        if r["expected_type"] is not None and r["object_type"] != r["expected_type"]
    ]

    # 2. Coordinates -------------------------------------------------------------------
    page = await ok(server, "get_level_objects", {"limit": 500})
    dy = set()
    for record in page.get("objects", []):
        raw = parse_save_string(record["save"])
        try:
            dy.add(round(float(record["properties"]["y"]) - float(raw[3]), 3))
        except (KeyError, ValueError):
            continue
    report["protocol_minus_save_y"] = sorted(dy)
    print(f"\n[2] protocol y minus save y: {sorted(dy)[:5]} "
          f"({'ok' if dy == {0.0} else 'UNEXPECTED' if dy else 'no objects'})")

    # 3. S-L1: the ground --------------------------------------------------------------
    print("\n[3] Ground height. In the editor, place ONE default block (the plain square)")
    print("    on the lowest row, resting on the ground, and leave it selected (only it).")
    await ask("    [Enter when done] ")
    picked = await selected(server)
    if len(picked) != 1:
        print(f"    {len(picked)} objects are selected; need exactly one. Skipping S-L1.")
        report["ground"] = {"error": f"{len(picked)} selected"}
    else:
        block = picked[0]
        y = float(block["y"])
        report["ground"] = {"id": block.get("id"), "block_y": y, "ground_top_y": y - 15.0}
        print(f"    block id {block.get('id')} at y = {y:g} -> ground top y = {y - 15:g}")

    # 4. S-L10: Edit Object ------------------------------------------------------------------
    print("\n[4] Revision. Keep that block selected for both steps.")
    checks = []
    for instruction, expected in [
        ("Open Edit Object on it, change its colour, close the popup.", True),
        ("Click empty space to deselect, then click the block to select it again.", False),
    ]:
        before = await revision(server)
        await ask(f"    {instruction}  [Enter when done] ")
        after = await revision(server)
        moved = before != after
        verdict = "ok" if moved == expected else "UNEXPECTED"
        print(f"      revision {before} -> {after}: {'moved' if moved else 'unchanged'} "
              f"({verdict})")
        checks.append({"step": instruction, "before": before, "after": after,
                       "moved": moved, "expected": expected})
    report["revision_checks"] = checks

    # 5. Camera ----------------------------------------------------------------------------
    print("\n[5] Camera. Scroll so the block is in the middle of the screen.")
    await ask("    [Enter when done] ")
    state = await ok(server, "get_editor_state")
    camera = state.get("camera")
    report["camera"] = camera
    if camera and "block_y" in report.get("ground", {}):
        print(f"    camera centre y = {camera['y']:g}, block y = {report['ground']['block_y']:g} "
              "(should be within a block or two)")
    else:
        print(f"    camera: {camera}")

    print("\nDone. You can delete the test block.")
    return report


def main() -> None:
    report = run(routine)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport written to {REPORT}")


if __name__ == "__main__":
    main()
