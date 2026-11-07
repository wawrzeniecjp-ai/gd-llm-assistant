"""Spike S-L3: can the mod drive the editor's playtest tick by tick? (LEVEL-DESIGN-SPEC.md 20)

Runs `run_replay` in the open level and reports what the bindings could not tell us:

1. **Ticks.** Does the tick hook fire during the editor's playtest, how often per second,
   and how many times per frame? (no input, 2 s)
2. **Determinism.** A single scripted jump, five times: the traces must agree to within
   0.01 units (the S-L3 exit criterion). Also gives a first look at the jump: initial
   velocity, gravity, apex, airtime.
3. **Death.** One spike on the ground ahead; the run must end with `outcome: death`
   and name the spike. The spike is removed afterwards.

Needs an **empty level** open in the editor (a fresh one is best): the player starts at
the level start and runs right, so anything in the first ~40 blocks changes the answer.
The playtest runs on screen, about 15 s in total; do not press anything while it runs
(a press cancels the replay, by design).

Run from `sidecar/`, with `start-sidecar.bat` closed:

    uv run python scripts/check_replay.py

The report is printed and written to `build/replay-report.json`.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from _harness import ok, run

from gd_ai_assistant.design.level_model import load_level_model
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.server import SidecarServer

REPORT = Path(__file__).resolve().parents[2] / "build" / "replay-report.json"
CLEAR_X = 1200          # the first 40 blocks must be free of gameplay objects
PRESS_TICK = 60
RUNS = 5
SPIKE_X = 615.0         # column 20, standing on the ground (y = 15)


def save(report: dict[str, Any]) -> None:
    """Written after every step, so a failure later on does not lose what was measured."""
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


async def replay(server: SidecarServer, payload: dict[str, Any]) -> dict[str, Any]:
    response = await server.request("run_replay", payload, timeout=90)
    if not response.ok:
        raise RuntimeError(f"run_replay failed: {response.error}")
    return response.result or {}


def _summary(result: dict[str, Any]) -> str:
    d = result.get("diagnostics", {})
    return (f"outcome={result.get('outcome')} ticks={result.get('ticks')} "
            f"rate={result.get('tick_rate_observed', 0):.1f}/s "
            f"update_calls={d.get('update_calls')} half_ticks={d.get('half_ticks')} "
            f"dt=[{d.get('dt_min', 0):.5f}..{d.get('dt_max', 0):.5f}] "
            f"progress {d.get('progress_first')}->{d.get('progress_last')} "
            f"wall={d.get('wall_seconds', 0):.2f}s")


def _jump_shape(trace: dict[str, list]) -> dict[str, Any]:
    """Read the jump off a trace: take-off tick, apex, airtime, and a gravity estimate."""
    ys, ticks, grounded = trace["y"], trace["tick"], trace["grounded"]
    base = ys[0]
    airborne = [i for i, g in enumerate(grounded) if not g and ticks[i] >= PRESS_TICK]
    if not airborne:
        return {"jumped": False}
    first, last = airborne[0], airborne[-1]
    while last + 1 < len(grounded) and not grounded[last + 1]:
        last += 1
    air = ys[first:last + 1]
    apex = max(air)
    # Second differences of y while airborne: constant if gravity is constant per tick.
    second = [air[i + 1] - 2 * air[i] + air[i - 1] for i in range(1, len(air) - 1)]
    return {
        "jumped": True,
        "takeoff_tick": ticks[first],
        "landing_tick": ticks[last] + 1,
        "airtime_ticks": ticks[last] + 1 - ticks[first],
        "apex_height": round(apex - base, 3),
        "first_dy": round(air[1] - air[0], 4) if len(air) > 1 else None,
        "vy_at_takeoff": trace["vy"][first],
        "second_difference_median": round(statistics.median(second), 5) if second else None,
        "second_difference_range": [round(min(second), 5), round(max(second), 5)]
        if second else None,
    }


async def routine(server: SidecarServer) -> dict[str, Any]:
    report: dict[str, Any] = {}
    model = await load_level_model(server, load_role_table())
    blocking = [o for o in model.gameplay() if o.x < CLEAR_X]
    if blocking:
        print(f"\nThe first {CLEAR_X // 30} blocks of this level hold {len(blocking)} gameplay "
              "objects. Open an empty level and run again.")
        return {"error": "level not empty"}

    # 1. Ticks ----------------------------------------------------------------------
    print("\n[1] No input, 2 s. Watch the playtest run; do not press anything.")
    idle = await replay(server, {"max_ticks": 480})
    print("    " + _summary(idle))
    xs = idle["trace"]["x"]
    if idle.get("outcome") == "no_ticks" or len(xs) < 3:
        print("    -> the tick hook never fired: processCommands is not the tick in playtest.")
        report["ticks"] = idle
        return report
    dx = [round(b - a, 4) for a, b in zip(xs, xs[1:], strict=False)]
    report["ticks"] = {
        "summary": _summary(idle),
        "diagnostics": idle.get("diagnostics"),
        "tick_rate_observed": idle.get("tick_rate_observed"),
        "dx_per_tick": sorted(set(dx))[:10],
        "speed_field": sorted(set(idle["trace"]["speed"]))[:5],
        "start": {"x": xs[0], "y": idle["trace"]["y"][0]},
        "x_first_40": xs[:40],
    }
    save(report)
    print(f"    dx per tick: {sorted(set(dx))[:6]}  start at ({xs[0]:g}, "
          f"{idle['trace']['y'][0]:g})")

    # 2. Determinism ------------------------------------------------------------------
    print(f"\n[2] One jump (press tick {PRESS_TICK}, release {PRESS_TICK + 1}), {RUNS} times.")
    script = {"inputs": [{"tick": PRESS_TICK, "action": "press"},
                         {"tick": PRESS_TICK + 1, "action": "release"}],
              "max_ticks": 360}
    runs = []
    for i in range(RUNS):
        result = await replay(server, script)
        runs.append(result)
        print(f"    run {i + 1}: " + _summary(result))
    reference = runs[0]["trace"]
    worst = {"x": 0.0, "y": 0.0}
    same_length = True
    for other in runs[1:]:
        trace = other["trace"]
        if len(trace["tick"]) != len(reference["tick"]):
            same_length = False
            continue
        for key in ("x", "y"):
            worst[key] = max(worst[key], max(abs(a - b) for a, b in
                                              zip(reference[key], trace[key], strict=True)))
    deterministic = same_length and worst["x"] <= 0.01 and worst["y"] <= 0.01
    jump = _jump_shape(reference)
    report["determinism"] = {"max_dx": worst["x"], "max_dy": worst["y"],
                             "same_length": same_length, "pass": deterministic,
                             "inputs_applied": runs[0].get("diagnostics", {}).get("inputs_applied")}
    report["jump"] = jump
    print(f"    worst difference across runs: x {worst['x']:.4f}, y {worst['y']:.4f} "
          f"-> {'PASS' if deterministic else 'FAIL'}")
    print(f"    jump: {jump}")
    report["jump_trace_first_run"] = {k: v[:240] for k, v in reference.items()}
    save(report)

    # 3. Death ---------------------------------------------------------------------------
    print("\n[3] A spike on the ground at x=615; the run should die on it.")
    txn = (await ok(server, "begin_transaction", {"label": "replay spike"}))["txn_id"]
    created = await ok(server, "create_objects", {"objects": [{"id": 8, "x": SPIKE_X, "y": 15}]})
    await ok(server, "end_transaction", {"txn_id": txn})
    spike_uid = created["created"][0]
    try:
        try:
            death = await replay(server, {"max_ticks": 720})
        except RuntimeError as exc:
            print(f"    {exc}")
            report["death"] = {"error": str(exc), "spike_uid": spike_uid}
            save(report)
            raise
        print("    " + _summary(death))
        print(f"    death: {death.get('death')} (spike uid {spike_uid})")
        report["death"] = {"outcome": death.get("outcome"), "death": death.get("death"),
                           "spike_uid": spike_uid,
                           "named_the_spike": (death.get("death") or {}).get("cause_uid")
                           == spike_uid}
    finally:
        reverted = await server.request("revert_transaction", {"txn_id": txn})
        if not reverted.ok:
            print(f"    revert refused ({reverted.error}); deleting the spike instead")
            txn2 = (await ok(server, "begin_transaction", {"label": "replay cleanup"}))["txn_id"]
            await ok(server, "delete_objects", {"targets": [spike_uid]})
            await ok(server, "end_transaction", {"txn_id": txn2})
        report["cleanup"] = "reverted" if reverted.ok else "deleted"

    print("\nDone.")
    return report


def main() -> None:
    report = run(routine)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport written to {REPORT}")


if __name__ == "__main__":
    main()
