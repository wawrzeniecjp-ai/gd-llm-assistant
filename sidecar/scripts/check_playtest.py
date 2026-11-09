"""M2: the solver's routes, played in the game (`playtest_verify`, LEVEL-DESIGN-SPEC.md 7.3).

Run from `sidecar/`, with `start-sidecar.bat` closed and a level open in the editor. Keep
your hands off the keyboard and mouse while it runs: any press takes over and cancels
the replay.

    uv run python scripts/check_playtest.py            # the sections level: slots 1-20
    uv run python scripts/check_playtest.py 3 6        # only slots 3 and 6
    uv run python scripts/check_playtest.py --edges    # also replay every window's edges
    uv run python scripts/check_playtest.py --whole    # any level: all of it, from the start

In the sections level, each slot is replayed from its own start position, up to six
columns past its last object. Impossible slots have no route, so nothing is played for
them. With `--edges`, each press is also replayed at both ends of its timing window and
one tick past each (four more runs per press), which checks the windows themselves.

Writes `build/playtest-report.json` (`build/playtest-level-report.json` with `--whole`, so
the two never overwrite each other). Wherever the game and the simulator part ways, or the
game dies, both traces go to `build/playtest-traces/sections/` (or `.../level/`) as CSV,
one file per run (`slot-06.csv`, `slot-06-press1-hi.csv`, ...), and the report names the
file. Each run clears only its own trace folder.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from _harness import run

from gd_ai_assistant.design.level_model import load_level_model
from gd_ai_assistant.design.roles import Role, load_role_table
from gd_ai_assistant.design.sim.playtest import TRACE_COLUMNS, playtest_verify
from gd_ai_assistant.server import SidecarServer

BUILD = Path(__file__).resolve().parents[2] / "build"
WHOLE = "--whole" in sys.argv[1:]
REPORT = BUILD / ("playtest-level-report.json" if WHOLE else "playtest-report.json")
TRACES = BUILD / "playtest-traces" / ("level" if WHOLE else "sections")
SLOT_COLS = 100
TAIL_COLS = 6

ARGS = sys.argv[1:]
EDGES = "--edges" in ARGS
ONLY = {int(a) for a in ARGS if a.isdigit()}


def _write_trace(name: str, rows: list[list[Any]]) -> str:
    TRACES.mkdir(parents=True, exist_ok=True)
    path = TRACES / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(TRACE_COLUMNS)
        writer.writerows(rows)
    return str(path.relative_to(BUILD.parent)).replace("\\", "/")


def _move_traces(name: str, result: dict[str, Any]) -> None:
    """Take the traces out of the result into CSV files; the report keeps their names."""
    comparison = result.get("comparison") or {}
    if "trace_rows" in comparison:
        comparison["trace_file"] = _write_trace(name, comparison.pop("trace_rows"))
    for edge in result.get("edges") or []:
        for check in edge["checks"]:
            if "trace_rows" in check:
                check["trace_file"] = _write_trace(
                    f"{name}-press{edge['press']}-{check['at']}", check.pop("trace_rows"))


def _line(label: str, result: dict[str, Any]) -> str:
    verdict = result.get("result")
    if verdict == "confirmed":
        comparison = result.get("comparison", {})
        detail = (f"{len(result['route'])} presses; game and simulator agree to "
                  f"{comparison.get('max_dx')} / {comparison.get('max_dy')} units")
        if not comparison.get("agrees", True):
            detail += f"; DIVERGES at {comparison.get('first_divergence')}"
        if "edges" in result:
            detail += "; edges " + ("agree" if result["edges_agree"] else "DISAGREE")
    elif verdict == "failed_in_game":
        death = (result.get("game") or {}).get("death") or {}
        detail = f"route died in the game at x {death.get('x')}"
    elif verdict == "error":
        detail = f"{result.get('code')}: {result.get('error')}"
    else:
        detail = result.get("note") or result.get("reason") or ""
    corrections = "; ".join(result.get("corrections", []))
    return f"  {label}: {verdict:22} {detail}" + (f"\n      ({corrections})" if corrections else "")


async def routine(server: SidecarServer) -> dict[str, Any]:
    model = await load_level_model(server, load_role_table())

    async def request(type_name: str, payload: dict[str, Any], timeout: float | None = None):
        return await server.request(type_name, payload, timeout=timeout)

    gameplay = [o for o in model if o.is_gameplay and o.role is not Role.START_POS]
    if not gameplay:
        raise SystemExit("The level has no gameplay objects.")
    rows = []
    shutil.rmtree(TRACES, ignore_errors=True)
    if WHOLE:
        end = max(o.x for o in gameplay) + TAIL_COLS * 30
        result = await playtest_verify(request, model, {
            "x_range": [0, end], "start": "level_start", "edges": EDGES, "max_replays": 200,
            "traces": True})
        _move_traces("level", result)
        print(_line("level", result))
        rows.append({"range": [0, end], "verification": result})
    else:
        for slot in range(1, 21):
            if ONLY and slot not in ONLY:
                continue
            x_slot = slot * SLOT_COLS * 30
            inside = [o for o in gameplay if x_slot <= o.x < x_slot + SLOT_COLS * 30]
            if not inside:
                print(f"  slot {slot:2}: empty")
                continue
            x_range = [x_slot + 60, max(o.x for o in inside) + TAIL_COLS * 30]
            result = await playtest_verify(request, model, {
                "x_range": x_range, "start": "auto", "edges": EDGES, "max_replays": 200,
                "traces": True})
            _move_traces(f"slot-{slot:02}", result)
            print(_line(f"slot {slot:2}", result))
            rows.append({"slot": slot, "range": x_range, "verification": result})
            if result.get("code") == "CANCELLED":
                print("Cancelled by a key press; stopping.")
                break
    confirmed = sum(1 for r in rows if r["verification"].get("result") == "confirmed")
    failed = sum(1 for r in rows if r["verification"].get("result") == "failed_in_game")
    edges_bad = sum(1 for r in rows if r["verification"].get("edges_agree") is False)
    print(f"\n{confirmed} confirmed in the game, {failed} failed in the game"
          + (f", {edges_bad} with window edges that disagree" if EDGES else ""))
    return {"edges": EDGES, "rows": rows, "confirmed": confirmed, "failed": failed}


def main() -> None:
    report = run(routine)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(f"Report written to {REPORT}")


if __name__ == "__main__":
    main()
