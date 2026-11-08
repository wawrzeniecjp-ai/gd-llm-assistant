"""M2 acceptance: does the solver classify 20 hand-made cube sections correctly?

The level layout is described in docs/M2-test-sections.md: 20 slots of 100 columns,
slot k starting at column 100*k. Slots 1-10 are meant to be possible, 11-20 impossible.
Each slot is verified from its own start (column 2, on the ground, cube at 1x) to the
end of its content area (column 95), with nothing before it mattering.

Run from `sidecar/`, with `start-sidecar.bat` closed and the test level open in the
editor (nothing is played or changed; the level is only read):

    uv run python scripts/check_sections.py

Writes `build/sections-report.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _harness import run

from gd_ai_assistant.design.level_model import load_level_model
from gd_ai_assistant.design.roles import Role, load_role_table
from gd_ai_assistant.design.sim.verify import verify_section
from gd_ai_assistant.server import SidecarServer

REPORT = Path(__file__).resolve().parents[2] / "build" / "sections-report.json"
SLOT_COLS = 100
SLOTS = range(1, 21)
START_COL = 2         # where the simulated cube starts in each slot
END_COL = 95          # where it must get to


def expected(slot: int) -> str:
    return "possible" if slot <= 10 else "impossible"


async def routine(server: SidecarServer) -> dict[str, Any]:
    model = await load_level_model(server, load_role_table())
    rows = []
    for slot in SLOTS:
        x_slot = slot * SLOT_COLS * 30
        x0, x1 = x_slot + START_COL * 30, x_slot + END_COL * 30
        content = [o for o in model.in_rect(x_slot, -1e9, x_slot + SLOT_COLS * 30, 1e9)
                   if o.is_gameplay and o.role is not Role.START_POS]
        if not content:
            rows.append({"slot": slot, "expected": expected(slot), "result": "empty"})
            print(f"  slot {slot:2}: empty")
            continue
        result = verify_section(model, {
            "x_range": [x0, x1], "start_from": "explicit",
            "start": {"mode": "cube", "speed": "1x", "row": 0},
        })
        verdict = result.get("result")
        passed = verdict == expected(slot)
        detail = ""
        if verdict == "possible":
            route = result.get("route", [])
            detail = (f"{len(route)} presses, tightest window "
                      f"{result['difficulty']['min_window_ms']} ms") if route else "no presses"
        elif verdict == "impossible":
            detail = (f"furthest x {result.get('furthest_x')} "
                      f"(column {int(result.get('furthest_x', x_slot) // 30) - slot * SLOT_COLS}"
                      f" of the slot), killed by {result.get('frontier_causes')}")
        elif verdict == "unverifiable":
            detail = "; ".join(sorted({u.get("reason", "") for u in result["unverifiable"]}))
        else:
            detail = result.get("reason", "")
        mark = "PASS" if passed else "FAIL"
        print(f"  slot {slot:2}: expected {expected(slot):10} got {verdict:13} {mark}  {detail}")
        rows.append({"slot": slot, "expected": expected(slot), "result": verdict,
                     "pass": passed, "detail": detail, "verification": result})
    checked = [r for r in rows if r["result"] != "empty"]
    passes = sum(1 for r in checked if r.get("pass"))
    print(f"\n{passes} of {len(checked)} sections classified correctly"
          + (f" ({len(rows) - len(checked)} slots empty)" if len(checked) < len(rows) else ""))
    return {"rows": rows, "passed": passes, "checked": len(checked)}


def main() -> None:
    report = run(routine)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"Report written to {REPORT}")


if __name__ == "__main__":
    main()
