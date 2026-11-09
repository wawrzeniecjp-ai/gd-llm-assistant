"""Calibrate the cube's physics from in-game replays (LEVEL-DESIGN-SPEC.md 7.4).

Builds each probe in the open level, replays scripted inputs through `run_replay`,
reads constants off the traces (`design/calibration.py`), and writes:

- `src/gd_ai_assistant/data/physics_constants.json`: the measured values, each with its
  probe as the source;
- `tests/fixtures/replays/cal_*.json`: every run as a golden trace for the simulator;
- `build/calibration-report.json`: everything, including the outcome tables (block
  landings, spike clearances) that the simulator is fitted to later.

Every probe's objects are created in a transaction and reverted afterwards, so the
level ends as it started.

Needs an **empty level** open in the editor. The playtest runs on screen for a few
minutes, around a hundred short runs; do not press anything while it runs (a press
cancels a run). Run from `sidecar/`, with `start-sidecar.bat` closed:

    uv run python scripts/calibrate.py              # every probe
    uv run python scripts/calibrate.py P6 P7        # only some
    uv run python scripts/calibrate.py P9           # the jump at 0.5x, 2x, 3x, 4x
"""

from __future__ import annotations

import contextlib
import datetime
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from _harness import ok, run

from gd_ai_assistant.design import calibration as cal
from gd_ai_assistant.design.constants import _default_path as constants_path
from gd_ai_assistant.design.level_model import load_level_model
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.server import SidecarServer

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "sidecar" / "tests" / "fixtures" / "replays"
REPORT = ROOT / "build" / "calibration-report.json"
TODAY = datetime.date.today().isoformat()
ONLY = {arg.upper() for arg in sys.argv[1:]}

CLEAR_X = 1200
COL = 8                      # obstacles sit in column 8: x 240..270, centre 255
X = 30 * COL + 15


def cell(col: int, row: int, y_offset: float = 0.0) -> tuple[float, float]:
    return 30 * col + 15, 15 + 30 * row + y_offset


def press(tick: int, hold: int = 1) -> list[dict[str, Any]]:
    return [{"tick": tick, "action": "press"}, {"tick": tick + hold, "action": "release"}]


@contextlib.asynccontextmanager
async def probe_level(server: SidecarServer, label: str,
                      objects: list[dict[str, Any]]) -> AsyncIterator[list[int]]:
    """Create a probe's objects for the duration of a block, then take them away."""
    if not objects:
        yield []
        return
    txn = (await ok(server, "begin_transaction", {"label": f"calibrate {label}"}))["txn_id"]
    created = await ok(server, "create_objects", {"objects": objects})
    await ok(server, "end_transaction", {"txn_id": txn})
    uids = list(created.get("created", []))
    try:
        yield uids
    finally:
        reverted = await server.request("revert_transaction", {"txn_id": txn})
        if not reverted.ok:
            cleanup = (await ok(server, "begin_transaction", {"label": "calibrate cleanup"}))
            await ok(server, "delete_objects", {"targets": uids})
            await ok(server, "end_transaction", {"txn_id": cleanup["txn_id"]})


class Runner:
    def __init__(self, server: SidecarServer) -> None:
        self.server = server
        self.failures = 0

    async def replay(self, inputs: list[dict[str, Any]], max_ticks: int) -> dict[str, Any]:
        response = await self.server.request(
            "run_replay", {"inputs": inputs, "max_ticks": max_ticks}, timeout=120)
        if not response.ok:
            self.failures += 1
            print(f"      run failed: {response.error}")
            if self.failures >= 3:
                raise RuntimeError("three runs failed; stopping (was something pressed?)")
            return {"error": response.error}
        self.failures = 0
        return response.result or {}


def run_record(inputs: list[dict[str, Any]], result: dict[str, Any], **extra: Any) -> dict:
    trace = result.get("trace") or {}
    record: dict[str, Any] = {"inputs": inputs, **extra}
    if "error" in result:
        record["error"] = result["error"]
        return record
    record.update({
        "ignore_damage_was_on": (result.get("diagnostics") or {}).get("ignore_damage_was_on"),
        "outcome": result.get("outcome"),
        "ticks": result.get("ticks"),
        "death": result.get("death"),
        "takeoffs": cal.takeoffs(trace),
        "impulses": cal.impulses(trace),
        "trace": cal.compact_trace(trace),
    })
    return record


def measured(value: float, probe: str) -> dict[str, Any]:
    return {"value": value, "source": f"scripts/calibrate.py {probe}, {TODAY}",
            "confidence": "measured"}


# -- probes ----------------------------------------------------------------------------------

SPEED_PORTALS = [("0.5x", 200), ("2x", 202), ("3x", 203), ("4x", 1334), ("1x", 201)]


async def probe_speeds(r: Runner, out: dict, constants: dict) -> None:
    """P1 + P8: a speed portal in column 6; the step before and after gives both speeds,
    and where the change happens gives the portal's timing."""
    runs = []
    for speed, object_id in SPEED_PORTALS:
        x, y = cell(6, 0)
        async with probe_level(r.server, f"speed {speed}", [{"id": object_id, "x": x, "y": y}]):
            result = await r.replay([], 400)
        trace = result.get("trace") or {}
        change = cal.speed_change(trace)
        if speed == "1x":
            per_tick = cal.dx_per_tick(trace, 5, 390)
        else:
            per_tick = change.after if change else None
        record = run_record([], result, speed=speed, portal={"id": object_id, "x": x, "y": y},
                            dx_per_tick=per_tick,
                            change_tick=change.tick if change else None,
                            change_x=change.x_at_change if change else None)
        runs.append(record)
        print(f"    {speed}: {per_tick} units/tick"
              + (f", changed at tick {change.tick} (x {change.x_at_change:.2f})" if change else ""))
        if per_tick:
            constants["units_per_second"][speed] = measured(round(per_tick * 240, 3), "P1/P8")
    out["P1_P8_speeds"] = runs
    save_fixture("cal_speeds", "P1/P8 speed portals", runs)


async def probe_jump(r: Runner, out: dict, cube: dict) -> None:
    """P2: one press on a flat floor."""
    inputs = press(60)
    result = await r.replay(inputs, 300)
    fit = cal.fit_jump(result.get("trace") or {})
    record = run_record(inputs, result, fit=fit.__dict__ if fit else None)
    out["P2_jump"] = record
    save_fixture("cal_jump", "P2 single jump", [record])
    if fit:
        print(f"    jump: vy0 {fit.jump_velocity}, gravity {fit.gravity}, scale {fit.scale}, "
              f"apex {fit.apex}, airtime {fit.airtime}, residual {fit.residual}")
        cube["jump_velocity"] = measured(fit.jump_velocity, "P2")
        cube["gravity"] = measured(fit.gravity, "P2")
        cube["velocity_scale"] = measured(fit.scale, "P2")
        cube["jump_apex"] = {**measured(fit.apex, "P2"), "confidence": "derived"}
        cube["jump_airtime_ticks"] = {**measured(fit.airtime, "P2"), "confidence": "derived"}


async def probe_jump_by_speed(r: Runner, out: dict, cube: dict) -> None:
    """P9: one press on a flat floor at each other speed (the jump is not the same).

    A speed portal in column 1 sets the speed long before the press. Found by playtest
    2026-11-09: at 0.5x the jump starts at vy 10.62 and gravity is 0.212, not 11.18 and
    0.216 as at 1x.
    """
    runs = []
    by_speed = cube.setdefault("by_speed", {})
    for speed, object_id in SPEED_PORTALS:
        if speed == "1x":
            continue
        x, y = cell(1, 0)
        inputs = press(120)
        async with probe_level(r.server, f"jump {speed}", [{"id": object_id, "x": x, "y": y}]):
            result = await r.replay(inputs, 420)
        fit = cal.fit_jump(result.get("trace") or {}, after_tick=110)
        runs.append(run_record(inputs, result, speed=speed, portal={"id": object_id, "x": x,
                                                                    "y": y},
                               fit=fit.__dict__ if fit else None))
        if not fit:
            print(f"    {speed}: no jump found in the trace")
            continue
        print(f"    {speed}: vy0 {fit.jump_velocity}, gravity {fit.gravity}, scale "
              f"{fit.scale}, apex {fit.apex}, airtime {fit.airtime}, residual {fit.residual}")
        by_speed[speed] = {"jump_velocity": measured(fit.jump_velocity, "P9"),
                           "gravity": measured(fit.gravity, "P9")}
    out["P9_jump_by_speed"] = runs
    save_fixture("cal_jump_speeds", "P9 single jump at each speed", runs)


async def probe_hold(r: Runner, out: dict, cube: dict) -> None:
    """P3: hold for a long time; does the cube jump again on landing, and when?"""
    inputs = press(60, hold=480)
    result = await r.replay(inputs, 600)
    trace = result.get("trace") or {}
    offs = cal.takeoffs(trace)
    fit = cal.fit_jump(trace)
    landing = fit.takeoff_tick + fit.airtime if fit else None
    record = run_record(inputs, result, landing_tick=landing)
    out["P3_hold"] = record
    save_fixture("cal_hold", "P3 hold to re-jump", [record])
    rejumps = len(offs) > 1
    delay = offs[1] - landing if rejumps and landing is not None else None
    print(f"    hold: take-offs at {offs}; first landing {landing}; re-jump delay {delay}")
    cube["rejump_on_landing"] = measured(1 if rejumps else 0, "P3")
    if delay is not None:
        cube["rejump_delay_ticks"] = measured(delay, "P3")


async def probe_block(r: Runner, out: dict) -> None:
    """P4: a one-block step in column 8, pressed at a sweep of ticks. The table of
    landed / died / cleared per press tick is what the simulator's landing tolerance
    and inner hitbox are fitted to."""
    x, y = cell(COL, 0)
    runs = []
    async with probe_level(r.server, "block", [{"id": 1, "x": x, "y": y}]):
        for tick in range(70, 183, 4):
            inputs = press(tick)
            result = await r.replay(inputs, 320)
            result_class = "error" if "error" in result else cal.outcome_of(result, landed_y=45.0)
            runs.append(run_record(inputs, result, press_tick=tick, classified=result_class))
            print(f"    press {tick}: {result_class}")
    out["P4_block"] = {"object": {"id": 1, "x": x, "y": y}, "runs": runs}
    save_fixture("cal_block", "P4 one-block step, press sweep", runs,
                 objects=[{"id": 1, "x": x, "y": y}])


async def probe_spike(r: Runner, out: dict) -> None:
    """P5: one spike in column 8, pressed at a sweep of ticks: which presses clear it."""
    x, y = cell(COL, 0)
    runs = []
    async with probe_level(r.server, "spike", [{"id": 8, "x": x, "y": y}]):
        for tick in range(110, 191, 3):
            inputs = press(tick)
            result = await r.replay(inputs, 300)
            result_class = "error" if "error" in result else cal.outcome_of(result)
            runs.append(run_record(inputs, result, press_tick=tick, classified=result_class))
            print(f"    press {tick}: {result_class}")
    if not any(run.get("classified") == "death" for run in runs):
        # Every timing cannot clear a spike at ground level: a late press runs into it.
        print("    WARNING: no run died. Damage is probably off in the game; these results "
              "are not usable.")
    out["P5_spike"] = {"object": {"id": 8, "x": x, "y": y}, "runs": runs}
    save_fixture("cal_spike", "P5 one spike, press sweep", runs,
                 objects=[{"id": 8, "x": x, "y": y}])


ORB_FIRST_PRESS = 143   # puts the jump's apex at the orb in column 8


async def probe_orb(r: Runner, out: dict, cube: dict, name: str, object_id: int,
                    step: int) -> None:
    """P6: jump towards an orb two rows up, then press again at a sweep of ticks. The
    second impulse gives the orb's strength; which presses fire it gives its reach. Two
    extra runs test buffering: holding through the orb, and pressing before it."""
    x, y = cell(COL, 2)
    runs = []
    impulses_seen = []
    async with probe_level(r.server, f"{name} orb", [{"id": object_id, "x": x, "y": y}]):
        for tick in range(ORB_FIRST_PRESS + 7, 231, step):
            inputs = press(ORB_FIRST_PRESS) + press(tick)
            result = await r.replay(inputs, 360)
            later = [i for i in cal.impulses(result.get("trace") or {})
                     if i[0] > ORB_FIRST_PRESS + 2]
            fired = bool(later)
            if fired:
                impulses_seen.append(later[0][1])
            runs.append(run_record(inputs, result, press_tick=tick, fired=fired,
                                   fired_at=later[0][0] if fired else None))
            print(f"    press {tick}: {'fired, vy ' + str(later[0][1]) if fired else 'no'}")
        buffering = {}
        for label, inputs in [
            ("held_through", press(ORB_FIRST_PRESS, hold=110)),
            ("pressed_before", press(ORB_FIRST_PRESS) + press(160, hold=100)),
        ]:
            result = await r.replay(inputs, 360)
            later = [i for i in cal.impulses(result.get("trace") or {})
                     if i[0] > ORB_FIRST_PRESS + 2]
            buffering[label] = later[0] if later else None
            runs.append(run_record(inputs, result, buffering=label,
                                   fired_at=later[0][0] if later else None))
            print(f"    {label}: {'fired at tick ' + str(later[0][0]) if later else 'no'}")
    out[f"P6_{name}_orb"] = {"object": {"id": object_id, "x": x, "y": y}, "runs": runs,
                             "buffering": buffering}
    save_fixture(f"cal_orb_{name}", f"P6 {name} orb, press sweep + buffering", runs,
                 objects=[{"id": object_id, "x": x, "y": y}])
    if impulses_seen:
        cube.setdefault("orb_impulse", {})[name] = measured(
            max(set(impulses_seen), key=impulses_seen.count), "P6")


async def probe_pad(r: Runner, out: dict, cube: dict, name: str, object_id: int,
                    y_offset: float) -> None:
    """P7: run over a pad, no input; the impulse is the pad's strength."""
    x, y = cell(COL, 0, y_offset)
    async with probe_level(r.server, f"{name} pad", [{"id": object_id, "x": x, "y": y}]):
        result = await r.replay([], 300)
    seen = cal.impulses(result.get("trace") or {})
    record = run_record([], result, pad={"id": object_id, "x": x, "y": y})
    out[f"P7_{name}_pad"] = record
    save_fixture(f"cal_pad_{name}", f"P7 {name} pad", [record],
                 objects=[{"id": object_id, "x": x, "y": y}])
    print(f"    {name} pad: {seen[0] if seen else 'no impulse'}")
    if seen:
        cube.setdefault("pad_impulse", {})[name] = measured(seen[0][1], "P7")


def save_fixture(name: str, probe: str, runs: list[dict], objects: list | None = None) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    data = {"probe": probe, "game_version": "2.2091", "recorded": TODAY,
            "objects": objects or [], "runs": runs}
    (FIXTURES / f"{name}.json").write_text(json.dumps(data) + "\n", encoding="utf-8")


def wanted(code: str) -> bool:
    return not ONLY or code in ONLY


async def routine(server: SidecarServer) -> dict[str, Any]:
    model = await load_level_model(server, load_role_table())
    if any(o.x < CLEAR_X for o in model.gameplay()):
        print(f"\nThe first {CLEAR_X // 30} blocks hold gameplay objects. Open an empty level.")
        return {"error": "level not empty"}

    path = constants_path()
    constants = json.loads(path.read_text(encoding="utf-8"))
    cube = constants.setdefault("modes", {}).setdefault("cube", {})
    r = Runner(server)
    out: dict[str, Any] = {"date": TODAY}
    if ONLY and REPORT.exists():
        # A partial rerun adds to the last report instead of replacing it.
        out = {**json.loads(REPORT.read_text(encoding="utf-8")), **out}

    steps = [
        ("P1", "speeds (P1/P8)", lambda: probe_speeds(r, out, constants)),
        ("P2", "single jump", lambda: probe_jump(r, out, cube)),
        ("P3", "hold", lambda: probe_hold(r, out, cube)),
        ("P4", "block landing sweep", lambda: probe_block(r, out)),
        ("P5", "spike sweep", lambda: probe_spike(r, out)),
        ("P6", "yellow orb", lambda: probe_orb(r, out, cube, "yellow", 36, 3)),
        ("P6", "pink orb", lambda: probe_orb(r, out, cube, "pink", 141, 6)),
        ("P7", "yellow pad", lambda: probe_pad(r, out, cube, "yellow", 35, -13.0)),
        ("P7", "pink pad", lambda: probe_pad(r, out, cube, "pink", 140, -13.0)),
        ("P9", "jump at each speed", lambda: probe_jump_by_speed(r, out, cube)),
    ]
    try:
        for code, label, step in steps:
            if not wanted(code):
                continue
            print(f"\n[{code}] {label}")
            await step()
            save_report(out)
    finally:
        constants["calibrated_at"] = TODAY
        for key, note in [
            ("landing_tolerance", "fitted by the simulator to the P4 outcome table (M2)"),
            ("inner_hitbox", "fitted by the simulator to the P4 outcome table (M2)"),
            ("terminal_velocity", "not reached by the Sim-1 probes"),
        ]:
            cube.setdefault(key, {"value": None, "source": note, "confidence": "guess"})
        path.write_text(json.dumps(constants, indent=4) + "\n", encoding="utf-8")
        save_report(out)
    print(f"\nConstants written to {path}\nFixtures in {FIXTURES}")
    return out


def save_report(out: dict[str, Any]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")


def main() -> None:
    run(routine)
    print(f"Report written to {REPORT}")


if __name__ == "__main__":
    main()
