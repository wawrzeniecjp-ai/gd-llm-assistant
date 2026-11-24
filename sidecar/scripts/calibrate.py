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
    uv run python scripts/calibrate.py P10          # landings near a block's edge
    uv run python scripts/calibrate.py P11          # what makes the stair landing differ
    uv run python scripts/calibrate.py P12          # jump snapping, every speed (~12 min)
    uv run python scripts/calibrate.py P13          # orbs and pads, every speed (~6 min)
    uv run python scripts/calibrate.py P14          # the timeline, five speed portals (~1 min)
    uv run python scripts/calibrate.py P15          # ship, ball, ufo, wave (~6 min)
    uv run python scripts/calibrate.py P16          # upside down, blue orb/pad, mini (~2 min)
    uv run python scripts/calibrate.py P17          # Sim-2 contacts and bands (~6 min)
    uv run python scripts/calibrate.py P18          # flight orbs/pads, mini, wave in solids
    uv run python scripts/calibrate.py P19          # the robot (~5 min)
    uv run python scripts/calibrate.py P20          # robot contacts (~6 min)
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
from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.level_model import load_level_model
from gd_ai_assistant.design.roles import Role, load_role_table
from gd_ai_assistant.design.sim.cube import HALF as HALF_SIM
from gd_ai_assistant.design.sim.cube import Cube, CubeParams, simulate
from gd_ai_assistant.design.sim.world import world_from_objects
from gd_ai_assistant.server import SidecarServer

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "sidecar" / "tests" / "fixtures" / "replays"
REPORT = ROOT / "build" / "calibration-report.json"
TODAY = datetime.date.today().isoformat()
ONLY = {arg.upper() for arg in sys.argv[1:]}

CLEAR_X = 1200
#: P10 builds far from the start too, where x is large (the game's x is a 32-bit float).
FAR_COL = 600
FAR_X = (30 * FAR_COL, 30 * (FAR_COL + 40))
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

    async def replay(self, inputs: list[dict[str, Any]], max_ticks: int,
                     start_pos: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"inputs": inputs, "max_ticks": max_ticks}
        if start_pos is not None:
            payload["start"] = {"kind": "start_pos", "uid": start_pos}
        response = await self.server.request("run_replay", payload, timeout=120)
        if not response.ok:
            self.failures += 1
            print(f"      run failed: {response.error}")
            if self.failures >= 3:
                raise RuntimeError("three runs failed; stopping (was something pressed?)")
            return {"error": response.error}
        self.failures = 0
        return response.result or {}


def run_record(inputs: list[dict[str, Any]], result: dict[str, Any], *, moded: bool = False,
               **extra: Any) -> dict:
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
        "trace": cal.compact_trace(trace, moded=moded),
    })
    return record


RAW = ROOT / "build" / "calibration-raw"


def save_raw(name: str, runs: list[dict[str, Any]]) -> None:
    """Every sample of every run, for fitting a mode's physics offline (Sim-2). Large, so
    it goes to build/, not the fixtures."""
    RAW.mkdir(parents=True, exist_ok=True)
    (RAW / f"{name}.json").write_text(json.dumps({"recorded": TODAY, "runs": runs}) + "\n",
                                      encoding="utf-8")


def _summary(trace: dict[str, Any]) -> str:
    ys, vys = trace.get("y") or [0], trace.get("vy") or [0]
    modes = list(dict.fromkeys(trace.get("mode") or []))
    flips = sum(1 for a, b in zip(trace.get("gravity", []), trace.get("gravity", [])[1:],
                                  strict=False) if a != b)
    return (f"modes {'>'.join(modes)}, y {min(ys):.1f}..{max(ys):.1f}, "
            f"vy {min(vys):.2f}..{max(vys):.2f}, gravity flips {flips}")


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


# P10/P11: the stair landing. Slot 6's second stair (a 2-block column at slot column 24,
# a 1-block step at 20, spikes between and after) moved the cube one unit further than
# its speed on the landing tick. P10 (2026-11-09) found it on every landing on that column,
# at any overhang, near or far, with or without spikes, and never on a lone block. But
# slot 6's 3- and 4-block steps (after 4-column gaps) did not do it, nor did a drop onto a
# 2-block top in slot 10. P11 changes one thing at a time from P10's stair.
#
# A variant: name, base column, start position?, objects as (id, col, row), the tops it
# must land on first (y of the cube), the target top (y), the target's column.
Variant = tuple[str, int, bool, list[tuple[int, int, int]], list[float], float, int]
_STEP, _COL2 = [(1, 20, 0)], [(1, 24, 0), (1, 24, 1)]
_SPIKES = [(8, c, 0) for c in (21, 22, 23, 25, 26, 27, 28)]
P10_VARIANTS: list[Variant] = [
    ("stair far", FAR_COL, True, _STEP + _COL2 + _SPIKES, [45.0], 75.0, 24),
    ("stair near", 0, False, _STEP + _COL2 + _SPIKES, [45.0], 75.0, 24),
    ("stair far, no spikes", FAR_COL, True, _STEP + _COL2, [45.0], 75.0, 24),
    ("block far", FAR_COL, True, _STEP, [], 45.0, 20),
]
P11_VARIANTS: list[Variant] = [
    ("A control: 2-block column, gap 3", FAR_COL, True, _STEP + _COL2, [45.0], 75.0, 24),
    ("B 2-block column, gap 4", FAR_COL, True, _STEP + [(1, 25, 0), (1, 25, 1)], [45.0],
     75.0, 25),
    ("C 3-block column, gap 3, from a 2-block step", FAR_COL, True,
     [(1, 16, 0), (1, 20, 0), (1, 20, 1), (1, 24, 0), (1, 24, 1), (1, 24, 2)],
     [45.0, 75.0], 105.0, 24),
    ("D 2-block column from the ground", FAR_COL, True, _COL2, [], 75.0, 24),
    ("E one floating block at row 1, gap 3", FAR_COL, True, _STEP + [(1, 24, 1)], [45.0],
     75.0, 24),
    ("F the third step of slot 6 (3 blocks, gap 4)", FAR_COL, True,
     _STEP + _COL2 + [(1, 29, 0), (1, 29, 1), (1, 29, 2)], [45.0, 75.0], 105.0, 29),
]


def _variant_objects(base: int, start_pos: bool,
                     parts: list[tuple[int, int, int]]) -> list[dict]:
    objects = []
    if start_pos:
        x, y = cell(base + 2, 0)
        objects.append({"id": 31, "x": x, "y": y})
    for object_id, col, row in parts:
        x, y = cell(base + col, row)
        objects.append({"id": object_id, "x": x, "y": y})
    return objects


def _sim_landing(world, params, start: Cube, presses: list[int], max_ticks: int):
    """The simulator's first landing after the last press (sim ticks), or None."""
    taps = [{"tick": t, "action": a} for r in presses for t, a in ((r, "press"),
                                                                   (r + 1, "release"))]
    sim = simulate(world, params, taps, max_ticks=max_ticks, start=start, start_tick=0)
    after = [lg for lg in cal.landings(sim.trace) if lg.tick > presses[-1] + 1]
    return after[0] if after else None


def _landing_plan(objects: list[dict], base: int, start_pos: bool, route_ys: list[float],
                  target_y: float, params: CubeParams, stride: int = 1) -> list[list[int]]:
    """Press ticks (sim ticks) that land the cube on the target top at every overhang
    the simulator allows (every `stride` ticks), plus one tick either side. The tops in
    `route_ys` are reached first, each with a press in the middle of the ticks that do."""
    world = world_from_objects([o for o in objects if o["id"] != 31], load_role_table())
    start = Cube(x=cell(base + 2, 0)[0], y=15.0, grounded=True) if start_pos else Cube()
    presses: list[int] = []
    for y in route_ys:
        begin = presses[-1] + 2 if presses else 1
        hits = [t for t in range(begin, begin + 900)
                if (lg := _sim_landing(world, params, start, [*presses, t], t + 200))
                and lg.y == y]
        if not hits:
            return []
        presses.append(hits[len(hits) // 2])
    begin = presses[-1] + 2 if presses else 1
    hits = [t for t in range(begin, begin + 900)
            if (lg := _sim_landing(world, params, start, [*presses, t], t + 200))
            and lg.y == target_y]
    if not hits:
        return []
    ticks = sorted({hits[0] - 1, *range(hits[0], hits[-1] + 1, stride), hits[-1],
                    hits[-1] + 1})
    return [[*presses, t] for t in ticks]


def _run_setup(objects: list[dict]) -> dict[str, Any]:
    """What a golden test needs to replay a run: its objects and its start position."""
    setup: dict[str, Any] = {"objects": [o for o in objects if o["id"] != 31]}
    starts = [o for o in objects if o["id"] == 31]
    if starts:
        setup["start"] = {"x": starts[0]["x"], "y": starts[0]["y"]}
    return setup


async def _probe_landings(r: Runner, out: dict, key: str, fixture: str, title: str,
                          variants: list[Variant], stride: int) -> None:
    params = CubeParams.from_constants(load_constants())
    runs = []
    for name, base, start_pos, parts, route_ys, target_y, target_col in variants:
        objects = _variant_objects(base, start_pos, parts)
        plans = _landing_plan(objects, base, start_pos, route_ys, target_y, params, stride)
        right_edge = 30 * (base + target_col + 1)
        print(f"    {name}: {len(plans)} runs (target top y {target_y - 15:.0f}, right edge "
              f"at x {right_edge})")
        if not plans:
            print("      the simulator finds no way onto the target; skipped")
            continue
        async with probe_level(r.server, f"landing {name}", objects) as uids:
            start_uid = uids[0] if start_pos and uids else None
            for presses in plans:
                # Replay ticks are one ahead of the simulator's (calibration P5).
                inputs = [i for t in presses for i in press(t + 1)]
                end = presses[-1] + 1 + 140
                result = await r.replay(inputs, end, start_pos=start_uid)
                trace = result.get("trace") or {}
                after = [lg for lg in cal.landings(trace) if lg.tick > presses[-1] + 2]
                landing = after[0] if after else None
                record = run_record(inputs, result, variant=name, right_edge=right_edge,
                                    target_y=target_y,
                                    landing=landing.__dict__ if landing else None,
                                    **_run_setup(objects))
                runs.append(record)
                if landing:
                    flag = "  <-- EXTRA" if abs(landing.extra) > 0.05 else ""
                    print(f"      press {presses[-1] + 1}: landed tick {landing.tick} y "
                          f"{landing.y:.1f}, centre {landing.x - right_edge:+.2f} past the "
                          f"edge, step {landing.step:.3f} ({landing.extra:+.3f}){flag}")
                else:
                    death = result.get("death") or {}
                    print(f"      press {presses[-1] + 1}: no landing ({result.get('outcome')}"
                          f" at x {death.get('x')})")
    out[key] = runs
    save_fixture(fixture, title, runs)
    on_target = [r for r in runs if r.get("landing") and r["landing"]["y"] == r["target_y"]]
    print("    landings on the target with an extra step, by variant:")
    for name, *_ in variants:
        mine = [r for r in on_target if r["variant"] == name]
        extra = sum(1 for r in mine if abs(r["landing"]["extra"]) > 0.05)
        print(f"      {name}: {extra} of {len(mine)}")


async def probe_landing_edge(r: Runner, out: dict) -> None:
    """P10: does landing near a block's right edge move the cube forward?"""
    await _probe_landings(r, out, "P10_landing_edge", "cal_landing_edge",
                          "P10 landings near a block's right edge", P10_VARIANTS, 1)


async def probe_landing_cause(r: Runner, out: dict) -> None:
    """P11: which part of P10's stair makes the extra step (see P11_VARIANTS)."""
    await _probe_landings(r, out, "P11_landing_cause", "cal_landing_cause",
                          "P11 what makes the stair landing step further", P11_VARIANTS, 2)


# P12: where jump snapping applies. P10/P11 found it for one spacing at 1x (120 across,
# 30 up: snaps; 150 across: does not). Two readings fit that: the game snaps only at fixed
# spacings, or whenever the landing is close to where the take-off spot maps on the new
# block. So the target is placed off the grid, at the jump's own flight distance plus
# offsets from -12 to +12 units (and at the nearest grid spacings), for each speed, going
# up a block, level, and down a block. Each run reports `needed` (how far the landing is
# from the mapped spot) and `extra` (what the game moved): a snap shows as extra = needed
# clamped to the step limit, no snap as extra 0.
P12_OFFSETS = (-12, -9, -6, -3, 0, 3, 6, 9, 12)
P12_KINDS = (("up", 0, 1), ("level", 0, 0), ("down", 1, 0))   # take-off top row, target row


def _flight(params: CubeParams, speed: str, rise: float) -> tuple[int, float]:
    """Ticks in the air and distance of a jump that lands `rise` units higher (or lower)."""
    vy, y, ticks = params.jump_for(speed), 0.0, 0
    while ticks < 1000:
        ticks += 1
        vy -= params.gravity_for(speed)
        if params.terminal_velocity is not None:
            vy = max(vy, -params.terminal_velocity)
        y += vy * params.scale
        if vy < 0 and y <= rise:
            break
    return ticks, ticks * params.dx[speed]


def _block(x: float, row: int) -> dict:
    return {"id": 1, "x": x, "y": 15 + 30 * row}


def _p12_layout(speed: str, portal_id: int, kind: str, takeoff_row: int, target_row: int,
                offset: float, params: CubeParams) -> tuple[list[dict], dict]:
    """Objects for one run, and where the take-off and target blocks are."""
    rise = 30.0 * (target_row - takeoff_row)
    _, flight = _flight(params, speed, rise)
    objects = [{"id": portal_id, "x": cell(1, 0)[0], "y": cell(1, 0)[1]}]
    takeoff_x = 30 * 20 + 15
    if takeoff_row == 1:
        # A step first, one up-jump before the 2-block take-off column.
        _, up = _flight(params, speed, 30.0)
        step_x = takeoff_x - 30 * max(3, round(up / 30))
        objects.append(_block(step_x, 0))
        objects += [_block(takeoff_x, 0), _block(takeoff_x, 1)]
        route = [45.0, 75.0]
    else:
        objects.append(_block(takeoff_x, 0))
        route = [45.0]
    spacing = round(flight + offset, 3)
    target_x = takeoff_x + spacing
    objects.append(_block(target_x, target_row))
    setup = {"speed": speed, "kind": kind, "offset": offset, "spacing": spacing,
             "flight": round(flight, 3), "takeoff": [takeoff_x, 15 + 30 * takeoff_row],
             "target": [target_x, 15 + 30 * target_row], "route_ys": route,
             "target_y": 15.0 + 30 * target_row + 30}
    return objects, setup


def _p12_plan(objects: list[dict], setup: dict, params: CubeParams,
              route: list[int] | None = None) -> list[int] | None:
    """Presses (sim ticks) that reach the take-off block and land on the target. `route`:
    the presses onto the take-off block, when already known (the same for every target)."""
    world = world_from_objects(objects, load_role_table())
    start = Cube()
    presses: list[int] = list(route or [])
    for y in ([] if route else setup["route_ys"]):
        begin = presses[-1] + 2 if presses else 1
        hits = [t for t in range(begin, begin + 1200)
                if (lg := _sim_landing(world, params, start, [*presses, t], t + 300))
                and lg.y == y]
        if not hits:
            return None
        presses.append(hits[len(hits) // 2])
    tx = setup["target"][0]
    begin = presses[-1] + 2
    hits = [t for t in range(begin, begin + 400)
            if (lg := _sim_landing(world, params, start, [*presses, t], t + 300))
            and lg.y == setup["target_y"] and abs(lg.x - tx) < 30]
    if not hits:
        return None
    return [*presses, hits[len(hits) // 2]]


def _p12_measure(trace: dict, setup: dict, last_press: int) -> dict | None:
    """The landing on the target: how far it was from the mapped take-off spot, and the
    game's extra step. Uses the full trace (every tick)."""
    after = [lg for lg in cal.landings(trace) if lg.tick > last_press + 1
             and lg.y == setup["target_y"]]
    if not after:
        return None
    landing = after[0]
    ticks, xs, gs = trace["tick"], trace["x"], trace["grounded"]
    k = ticks.index(landing.tick)
    j = k - 1
    while j > 0 and not gs[j]:
        j -= 1
    takeoff_x = xs[j]
    before = landing.x - landing.extra
    mapped = setup["target"][0] + (takeoff_x - setup["takeoff"][0])
    return {"tick": landing.tick, "x": landing.x, "extra": landing.extra,
            "takeoff_x": takeoff_x, "needed": round(mapped - before, 3),
            "air_ticks": landing.tick - ticks[j]}


async def probe_jump_snap(r: Runner, out: dict) -> None:
    """P12: which landings snap, at every speed (see the note above P12_OFFSETS)."""
    params = CubeParams.from_constants(load_constants())
    runs = []
    for speed, portal_id in SPEED_PORTALS:
        for kind, takeoff_row, target_row in P12_KINDS:
            rise = 30.0 * (target_row - takeoff_row)
            _, flight = _flight(params, speed, rise)
            offsets = list(P12_OFFSETS)
            for grid in (30 * (flight // 30), 30 * (flight // 30 + 1)):
                if abs(grid - flight) <= 20 and all(abs(grid - flight - o) > 1 for o in offsets):
                    offsets.append(round(grid - flight, 3))
            print(f"    {speed} {kind}: flight {flight:.1f}, {len(offsets)} spacings")
            route: list[int] | None = None
            for offset in sorted(offsets):
                objects, setup = _p12_layout(speed, portal_id, kind, takeoff_row, target_row,
                                             offset, params)
                presses = _p12_plan(objects, setup, params, route)
                if presses is not None:
                    route = presses[:-1]
                if presses is None:
                    print(f"      spacing {setup['spacing']:.1f}: the simulator finds no way; "
                          "skipped")
                    continue
                inputs = [i for t in presses for i in press(t + 1)]
                async with probe_level(r.server, f"snap {speed} {kind}", objects):
                    result = await r.replay(inputs, presses[-1] + 1 + 200)
                trace = result.get("trace") or {}
                measure = _p12_measure(trace, setup, presses[-1] + 1) if trace else None
                runs.append(run_record(inputs, result, objects=objects, **setup,
                                       measure=measure))
                if measure is None:
                    print(f"      spacing {setup['spacing']:.1f}: no landing on the target "
                          f"({result.get('outcome')})")
                    continue
                flag = "  <-- SNAP" if abs(measure["extra"]) > 0.05 else ""
                print(f"      spacing {setup['spacing']:7.1f} (offset {offset:+6.1f}): needed "
                      f"{measure['needed']:+7.3f}, game moved {measure['extra']:+.3f}{flag}")
    out["P12_jump_snap"] = runs
    save_fixture("cal_jump_snap", "P12 jump snapping by speed and spacing", runs)
    print("    snapped landings by speed and kind:")
    for speed, _ in SPEED_PORTALS:
        for kind, *_ in P12_KINDS:
            mine = [r for r in runs if r["speed"] == speed and r["kind"] == kind
                    and r.get("measure")]
            snapped = [r for r in mine if abs(r["measure"]["extra"]) > 0.05]
            print(f"      {speed:4} {kind:5}: {len(snapped)} of {len(mine)}")


# P13: orbs and pads at every speed. P6/P7 measured them at 1x only, and the jump turned
# out to differ by speed (P9), so their impulses may too. A speed portal in column 1, the
# object in column 12. Pads: run over it. Orbs (two rows up): the simulator picks a jump
# that passes through the orb, then a second press on every tick it expects the orb to
# fire, plus one either side; the game's impulse is the orb's strength at that speed, and
# which presses fire gives its reach.
P13_COL = 12
P13_OBJECTS = (("yellow orb", 36, 2, 0.0), ("pink orb", 141, 2, 0.0),
               ("yellow pad", 35, 0, -13.0), ("pink pad", 140, 0, -13.0))


def _airborne_impulses(trace: dict, after: int) -> list[tuple[int, float]]:
    """Impulses that start in the air (an orb or a pad), not jumps off the ground."""
    ticks, gs = trace.get("tick") or [], trace.get("grounded") or []
    grounded_at = dict(zip(ticks, gs, strict=False))
    return [(t, vy) for t, vy in cal.impulses(trace)
            if t > after and not grounded_at.get(t - 1, 1)]


def _p13_orb_plan(objects: list[dict], params: CubeParams) -> list[list[int]]:
    """Replay-tick presses: a jump through the orb, then each candidate second press."""
    world = world_from_objects(objects, load_role_table())
    orb = next(b for b in world.bodies if b.role is Role.ORB)

    def touching(presses: list[int], max_ticks: int) -> list[int]:
        taps = [{"tick": t, "action": a} for r in presses for t, a in ((r, "press"),
                                                                       (r + 1, "release"))]
        sim = simulate(world, params, taps, max_ticks=max_ticks)
        return [t for t, x, y in zip(sim.trace["tick"], sim.trace["x"], sim.trace["y"],
                                     strict=True)
                if orb.touches(Cube(x=x, y=y).box())]

    best: tuple[int, int] | None = None
    for first in range(2, 900):
        hit = touching([first], first + 200)
        if hit and (best is None or len(hit) > best[1]):
            best = (first, len(hit))
        elif best and not hit and first > best[0] + 40:
            break
    if best is None:
        return []
    first = best[0]
    # Only presses while the first jump is still in the air: a later one jumps off the
    # ground through the orb again, which is not the orb's reach.
    lone = simulate(world, params, [{"tick": first, "action": "press"},
                                    {"tick": first + 1, "action": "release"}],
                    max_ticks=first + 300)
    landed = next((lg.tick for lg in cal.landings(lone.trace) if lg.tick > first + 2),
                  first + 200)
    fires = []
    for second in range(first + 3, landed):
        taps = [{"tick": t, "action": a} for r in (first, second)
                for t, a in ((r, "press"), (r + 1, "release"))]
        sim = simulate(world, params, taps, max_ticks=second + 4)
        if orb.uid in sim.final.used:
            fires.append(second)
    if not fires:
        return []
    # Every tick at both ends of the reach (where it starts and stops firing), every fourth
    # in between: the impulse is the same throughout.
    ends = set(range(fires[0] - 1, fires[0] + 3)) | set(range(fires[-1] - 2, fires[-1] + 2))
    middle = set(range(fires[0] + 3, fires[-1] - 2, 4))
    return [[first, t] for t in sorted(ends | middle)]


async def probe_orbs_pads_by_speed(r: Runner, out: dict, cube: dict) -> None:
    """P13: orb and pad impulses at every speed (see the note above P13_COL)."""
    params = CubeParams.from_constants(load_constants())
    runs = []
    by_speed = cube.setdefault("by_speed", {})
    for speed, portal_id in SPEED_PORTALS:
        for name, object_id, row, y_offset in P13_OBJECTS:
            color, kind = name.split()
            px, py = cell(1, 0)
            ox, oy = cell(P13_COL, row, y_offset)
            objects = [{"id": portal_id, "x": px, "y": py}, {"id": object_id, "x": ox, "y": oy}]
            plans = _p13_orb_plan(objects, params) if kind == "orb" else [[]]
            if not plans:
                print(f"    {speed} {name}: the simulator finds no jump through it; skipped")
                continue
            seen = []
            async with probe_level(r.server, f"{name} {speed}", objects):
                for presses in plans:
                    inputs = [i for t in presses for i in press(t)]
                    result = await r.replay(inputs, (presses[-1] if presses else 0) + 400)
                    trace = result.get("trace") or {}
                    if kind == "orb":
                        later = _airborne_impulses(trace, presses[0] + 2)
                    else:
                        later = [i for i in cal.impulses(trace) if i[0] > 0]
                    fired = later[0] if later else None
                    if fired:
                        seen.append(fired[1])
                    runs.append(run_record(inputs, result, objects=objects, speed=speed,
                                           object=name, press_tick=presses[-1] if presses
                                           else None, fired=bool(fired),
                                           impulse=fired[1] if fired else None))
            fired_at = [rr["press_tick"] for rr in runs[-len(plans):] if rr["fired"]]
            value = max(set(seen), key=seen.count) if seen else None
            where = f", fired for presses {fired_at[0]}-{fired_at[-1]}" if kind == "orb" \
                and fired_at else ""
            print(f"    {speed} {name}: vy {value}{where} ({len(seen)} of {len(plans)} runs)")
            if value is not None:
                table = by_speed.setdefault(speed, {}).setdefault(f"{kind}_impulse", {})
                table[color] = measured(value, "P13")
    out["P13_orbs_pads"] = runs
    save_fixture("cal_orbs_pads_speeds", "P13 orbs and pads at every speed", runs)


# P14: the timeline (LEVEL-DESIGN-SPEC.md 8.1, M4's done-when). Five speed portals along a
# flat floor, every speed, no input: where the game's cube is on each tick against where
# `design/timeline.py` says it is. Done when every x is reached within one tick.
P14_COURSE = [(20, "2x"), (50, "0.5x"), (70, "3x"), (110, "4x"), (160, "1x")]
P14_END_X = 6000


async def probe_timeline(r: Runner, out: dict) -> None:
    """P14: the timeline against the game at five speed portals."""
    import math

    from gd_ai_assistant.design.sim.cube import HALF
    from gd_ai_assistant.design.timeline import Timeline

    ids = dict(SPEED_PORTALS)
    objects = []
    for col, speed in P14_COURSE:
        x, y = cell(col, 1)
        objects.append({"id": ids[speed], "x": x, "y": y})
    params = CubeParams.from_constants(load_constants())
    world = world_from_objects(objects, load_role_table())
    timeline = Timeline.build([(b.box.x1 - HALF, b.variant, b.uid) for b in world.bodies],
                              params)
    async with probe_level(r.server, "timeline", objects):
        result = await r.replay([], 4200)
    trace = result.get("trace") or {}
    errors = []
    for x_probe in range(100, P14_END_X, 97):
        tick = next((t for t, x in zip(trace.get("tick", []), trace.get("x", []), strict=True)
                     if x >= x_probe), None)
        if tick is None:
            break
        # Sample 1 is the level start's command phase, with no move.
        predicted = math.ceil(timeline.ms_at(x_probe) * 0.24 + 1 - 1e-9)
        errors.append((x_probe, tick - predicted))
    worst = max((abs(e) for _, e in errors), default=None)
    print(f"    {len(errors)} points; worst difference {worst} tick(s)"
          + ("" if worst is not None and worst <= 1 else "  <-- MORE THAN ONE TICK"))
    for x_probe, error in errors:
        if abs(error) > 1:
            print(f"      x {x_probe}: game {error:+d} ticks from the timeline")
    out["P14_timeline"] = {"errors": errors, "worst": worst,
                           "segments": timeline.describe()}
    save_fixture("cal_timeline", "P14 timeline at five speed portals",
                 [run_record([], result, objects=objects)])


# -- Sim-2 exploration (M5) ---------------------------------------------------------------
#
# P15 and P16 do not fit anything: they record full traces of each new mode and feature
# under simple inputs, so the physics can be read off the data offline rather than
# guessed (LEVEL-DESIGN-SPEC.md 6.5: "not to be guessed"). Full traces go to
# build/calibration-raw/, compact ones (with mode, gravity and mini) to the fixtures.

MODE_PORTALS = {"ship": 13, "ball": 47, "ufo": 111, "wave": 660}
GRAVITY_FLIP, GRAVITY_NORMAL, GRAVITY_TOGGLE = 11, 10, 2926
SIZE_MINI, SIZE_NORMAL = 101, 99
P15_START = 320                                  # first input, well after every portal
P15_INPUTS: dict[str, tuple[list[dict[str, Any]], int]] = {
    "idle": ([], 800),
    "hold": (press(P15_START, hold=150), 900),
    "taps": ([i for k in range(5) for i in press(P15_START + 60 * k)], 900),
    "hold_long": (press(P15_START, hold=700), 1200),
}
#: (label, speed, mini, flipped, high portal, inputs)
P15_VARIANTS = [
    ("normal", "1x", False, False, False, ("hold", "taps", "hold_long")),
    ("mini", "1x", True, False, False, ("hold", "taps")),
    ("flipped", "1x", False, True, False, ("idle", "hold", "taps")),
    *[(f"speed {s}", s, False, False, False, ("hold", "taps"))
      for s in ("0.5x", "2x", "3x", "4x")],
    ("high portal", "1x", False, False, True, ("idle", "hold_long")),
]
PAD_Y_OFFSET = -13.0                              # a pad on the floor, as P7


def _high_portal_y() -> float:
    """Where a cube launched by a yellow pad in column 3 is as it reaches column 6, to the
    nearest half cell: the high portal sits there, so its mode's bounds are measured
    away from the ground too."""
    x, y = cell(3, 0, PAD_Y_OFFSET)
    world = world_from_objects([{"id": 35, "x": x, "y": y}], load_role_table())
    run = simulate(world, CubeParams.from_constants(load_constants()), [], max_ticks=400,
                   until_x=cell(6, 0)[0])
    return max(45.0, round(run.final.y / 15) * 15)


def _p15_layout(mode: str, speed: str, mini: bool, flipped: bool, high: bool) -> list[dict]:
    objects: list[dict[str, Any]] = []
    ids = dict(SPEED_PORTALS)
    if speed != "1x":
        x, y = cell(2, 1)
        objects.append({"id": ids[speed], "x": x, "y": y})
    if mini:
        x, y = cell(4, 1)
        objects.append({"id": SIZE_MINI, "x": x, "y": y})
    if high:
        x, y = cell(3, 0, PAD_Y_OFFSET)
        objects.append({"id": 35, "x": x, "y": y})
        objects.append({"id": MODE_PORTALS[mode], "x": cell(6, 0)[0], "y": _high_portal_y()})
    else:
        x, y = cell(6, 1)
        objects.append({"id": MODE_PORTALS[mode], "x": x, "y": y})
    if flipped:
        x, y = cell(9, 1)
        objects.append({"id": GRAVITY_FLIP, "x": x, "y": y})
    return objects


async def probe_modes(r: Runner, out: dict) -> None:
    """P15: ship, ball, ufo and wave under simple inputs: held, tapped, held long; mini,
    upside down, at every speed, and through a portal high above the ground."""
    runs, raw = [], []
    for mode in MODE_PORTALS:
        for label, speed, mini, flipped, high, inputs_wanted in P15_VARIANTS:
            objects = _p15_layout(mode, speed, mini, flipped, high)
            async with probe_level(r.server, f"{mode} {label}", objects):
                for name in inputs_wanted:
                    inputs, max_ticks = P15_INPUTS[name]
                    result = await r.replay(inputs, max_ticks)
                    meta = {"mode": mode, "variant": label, "speed": speed, "mini": mini,
                            "flipped": flipped, "high": high, "inputs_name": name}
                    runs.append(run_record(inputs, result, moded=True, objects=objects,
                                           **meta))
                    raw.append({**meta, "inputs": inputs, "objects": objects,
                                "outcome": result.get("outcome"), "death": result.get("death"),
                                "trace": result.get("trace")})
                    print(f"    {mode:4} {label:11} {name:9}: {result.get('outcome')} at "
                          f"{result.get('ticks')}; {_summary(result.get('trace') or {})}")
    out["P15_modes"] = [{k: v for k, v in run.items() if k != "trace"} for run in runs]
    save_fixture("cal_modes", "P15 ship, ball, ufo and wave under simple inputs", runs)
    save_raw("P15", raw)


P16_CEILING = [(c, 7) for c in range(4, 46)]     # a roof, so a flipped cube lands on it


def _roof() -> list[dict[str, Any]]:
    return [{"id": 1, "x": cell(c, row)[0], "y": cell(c, row)[1]} for c, row in P16_CEILING]


async def probe_gravity(r: Runner, out: dict) -> None:
    """P16: the cube upside down (gravity portals, blue pad, blue orb) and mini."""
    taps = press(P15_START) + press(P15_START + 100)
    gx, gy = cell(6, 1)
    cases: list[tuple[str, list[dict], list[list[dict]], int]] = [
        ("gravity portals", _roof() + [{"id": GRAVITY_FLIP, "x": gx, "y": gy},
                                       {"id": GRAVITY_NORMAL, "x": cell(30, 6)[0],
                                        "y": cell(30, 6)[1]}], [taps], 900),
        ("toggle portal", _roof() + [{"id": GRAVITY_TOGGLE, "x": gx, "y": gy}], [[]], 500),
        ("blue pad", _roof() + [{"id": 67, "x": cell(COL, 0, PAD_Y_OFFSET)[0],
                                 "y": cell(COL, 0, PAD_Y_OFFSET)[1]}], [taps], 700),
        ("blue orb", _roof() + [{"id": 84, "x": cell(COL, 2)[0], "y": cell(COL, 2)[1]}],
         [press(ORB_FIRST_PRESS) + press(t) for t in range(150, 231, 8)]
         + [press(ORB_FIRST_PRESS, hold=110)], 600),
        ("mini", [{"id": SIZE_MINI, "x": gx, "y": gy},
                  {"id": 35, "x": cell(30, 0, PAD_Y_OFFSET)[0],
                   "y": cell(30, 0, PAD_Y_OFFSET)[1]},
                  {"id": 36, "x": cell(40, 2)[0], "y": cell(40, 2)[1]}],
         [press(P15_START), press(P15_START, hold=200), []], 1100),
    ]
    runs, raw = [], []
    for label, objects, input_sets, max_ticks in cases:
        async with probe_level(r.server, label, objects):
            for inputs in input_sets:
                result = await r.replay(inputs, max_ticks)
                runs.append(run_record(inputs, result, moded=True, objects=objects,
                                       variant=label))
                raw.append({"variant": label, "inputs": inputs, "objects": objects,
                            "outcome": result.get("outcome"), "death": result.get("death"),
                            "trace": result.get("trace")})
                first = inputs[-2]["tick"] if len(inputs) >= 2 else None
                print(f"    {label:15} last press {first}: {result.get('outcome')} at "
                      f"{result.get('ticks')}; {_summary(result.get('trace') or {})}")
    out["P16_gravity"] = [{k: v for k, v in run.items() if k != "trace"} for run in runs]
    save_fixture("cal_gravity", "P16 cube upside down, blue pad and orb, mini", runs)
    save_raw("P16", raw)


# P17 (M5): what Sim-2 still reports as unverifiable. The simulator itself (now flying
# ship, ball, ufo and wave) aims each run, so a portal sits where the player will be.

CUBE_PORTAL = 12
#: Inputs that leave each mode moving when it reaches column 12 (P17 A).
P17_RETURN_INPUTS = {
    "ship": [press(140, hold=60)],                  # rising, then slowing
    "ship falling": [press(140, hold=40)],          # up, released, falling by column 12
    "ball": [press(240)],                           # flipped, falling up
    "ufo": [press(250)],                            # just jumped
    "wave": [press(140, hold=200)],                 # climbing
}
#: When a ship holding from tick 140 lets go; the simulator puts it at column 20 at
#: these heights, rising or falling (P17 B).
P17_BAND_RELEASES = (260, 275, 290, 440)    # portal at y ~143, 192, 238, 284
P17_BAND_MODES = ("ufo", "wave", "ball")
#: P17 C/D: a spike or a block above a player gliding on the floor, its bottom this far
#: from the player's top (outer box); the wave's top is taken at its rest + 10.
P17_CLEARANCES = {"ship": range(-6, 5), "ufo": range(-6, 5), "ball": range(-6, 5),
                  "wave": range(-14, 3, 2)}
P17_BLOCK_CLEARANCES = range(-6, 3, 2)


def _sim_y_at(objects: list[dict], inputs: list[dict], x: float) -> float | None:
    """Where the simulator puts the player as it reaches `x`, or None if it cannot."""
    world = world_from_objects(objects, load_role_table())
    run = simulate(world, CubeParams.from_constants(load_constants()), inputs,
                   max_ticks=3000, until_x=x)
    return run.final.y if run.outcome == "reached" else None


async def _record(r: Runner, runs: list, raw: list, label: str, objects: list[dict],
                  inputs: list[dict], max_ticks: int, **meta: Any) -> None:
    async with probe_level(r.server, label, objects):
        result = await r.replay(inputs, max_ticks)
    runs.append(run_record(inputs, result, moded=True, objects=objects, variant=label,
                           **meta))
    raw.append({"variant": label, **meta, "inputs": inputs, "objects": objects,
                "outcome": result.get("outcome"), "death": result.get("death"),
                "trace": result.get("trace")})
    death = result.get("death") or {}
    print(f"    {label:28}: {result.get('outcome')} at {result.get('ticks')}"
          + (f" (x {death.get('x', 0):.1f}, y {death.get('y', 0):.1f})" if death else "")
          + f"; {_summary(result.get('trace') or {})}")


async def probe_contacts(r: Runner, out: dict) -> None:
    """P17: back to the cube from each mode (A); mode portals above the ground (B); a
    spike (C) and a block (D) just above each mode gliding on the floor."""
    runs: list[dict] = []
    raw: list[dict] = []
    mx, my = cell(6, 1)
    # A: the cube portal at column 12, centred where the simulator puts the player.
    portal_x = cell(12, 0)[0]
    for name, inputs_list in P17_RETURN_INPUTS.items():
        mode = name.split()[0]
        base = [{"id": MODE_PORTALS[mode], "x": mx, "y": my}]
        inputs = inputs_list[0] if len(inputs_list) == 1 else inputs_list
        y = _sim_y_at(base, inputs, portal_x - 17 - HALF_SIM)
        if y is None:
            print(f"    {name}: the simulator could not reach column 12; skipped")
            continue
        objects = [*base, {"id": CUBE_PORTAL, "x": portal_x, "y": round(y, 1)}]
        await _record(r, runs, raw, f"A {name} to cube", objects, inputs, 700, part="A",
                      mode=mode)
    # B: a ship climbs, lets go, and falls past a portal of another mode in column 20.
    bx = cell(20, 0)[0]
    ship = [{"id": MODE_PORTALS["ship"], "x": mx, "y": my}]
    for release in P17_BAND_RELEASES:
        inputs = [{"tick": 140, "action": "press"}, {"tick": release, "action": "release"}]
        y = _sim_y_at(ship, inputs, bx - 17 - HALF_SIM)
        if y is None:
            continue
        for target in P17_BAND_MODES:
            objects = [*ship, {"id": MODE_PORTALS[target], "x": bx, "y": round(y, 1)}]
            await _record(r, runs, raw, f"B {target} portal at y {y:.0f}", objects,
                          inputs, 1000, part="B", mode=target, portal_y=round(y, 1))
    # C and D: on the floor in each mode, something just above at column 14.
    sx = cell(14, 0)[0]
    for mode, clearances in P17_CLEARANCES.items():
        top = 10.0 + 10.0 if mode == "wave" else 15.0 + 15.0
        base = [{"id": MODE_PORTALS[mode], "x": mx, "y": my}]
        for d in clearances:
            spike = {"id": 8, "x": sx, "y": top + d + 6.0}      # hitbox 6 below centre
            await _record(r, runs, raw, f"C {mode} spike {d:+d}", [*base, spike], [], 400,
                          part="C", mode=mode, clearance=d)
        for d in P17_BLOCK_CLEARANCES:
            block = {"id": 1, "x": sx, "y": top + d + 15.0}
            await _record(r, runs, raw, f"D {mode} block {d:+d}", [*base, block], [], 400,
                          part="D", mode=mode, clearance=d)
    out["P17_contacts"] = [{k: v for k, v in run.items() if k != "trace"} for run in runs]
    save_fixture("cal_contacts", "P17 back to the cube, bands, spikes and blocks above", runs)
    save_raw("P17", raw)


# P18 (M5): the rest of what Sim-2 reports as unverifiable: orbs and pads in the flight
# modes (A, B), a mini player meeting spikes and blocks (C, D, E), and the wave meeting
# a block's face or corner (F). As in P17, the simulator aims each run from the layout
# without the object being measured, which it can already fly.

ORB_IDS = {"yellow": 36, "pink": 141, "blue": 84}
PAD_IDS = {"yellow": 35, "pink": 140, "blue": 67}
FLIGHT_MODES = ("ship", "ufo", "ball", "wave")
P18_COL = 14                                  # the measured object's column
P18_PAD_SPEEDS = ("1x", "2x")
#: Mini: the player's top (outer box) gliding on the floor; the wave's, as in P17, at
#: twice its half size.
P18_MINI_TOP = {"cube": 18.0, "ship": 18.0, "ufo": 18.0, "ball": 18.0, "wave": 12.0}
P18_MINI_CLEARANCES = {"cube": range(-6, 5, 2), "ship": range(-6, 5), "ufo": range(-6, 5),
                       "ball": range(-6, 5), "wave": range(-10, 3, 2)}
P18_MINI_BLOCK_CLEARANCES = range(-6, 3, 2)
#: F: the wave climbs from this tick (well after its portal); its centre crosses a
#: block's face this far either side of the block's corner (about 1.3 units a tick).
P18_WAVE_CLIMB = 200
P18_CORNER_REACH = 24.0
P18_CORNER_OFFSETS = range(-24, 25, 4)


def _p18_base(mode: str, mini: bool = False, speed: str = "1x") -> list[dict[str, Any]]:
    """A speed portal (column 2), mini (4) and the mode (6), each only when needed."""
    objects: list[dict[str, Any]] = []
    if speed != "1x":
        x, y = cell(2, 1)
        objects.append({"id": dict(SPEED_PORTALS)[speed], "x": x, "y": y})
    if mini:
        x, y = cell(4, 1)
        objects.append({"id": SIZE_MINI, "x": x, "y": y})
    if mode != "cube":
        x, y = cell(6, 1)
        objects.append({"id": MODE_PORTALS[mode], "x": x, "y": y})
    return objects


def _sim_run(objects: list[dict], inputs: list[dict], max_ticks: int = 3000):
    world = world_from_objects(objects, load_role_table())
    return simulate(world, CubeParams.from_constants(load_constants()), inputs,
                    max_ticks=max_ticks)


def _sim_tick_at(objects: list[dict], inputs: list[dict], x: float) -> int | None:
    """The tick the simulator's player reaches `x`, or None if it cannot."""
    world = world_from_objects(objects, load_role_table())
    run = simulate(world, CubeParams.from_constants(load_constants()), inputs,
                   max_ticks=3000, until_x=x)
    return run.ticks if run.outcome == "reached" else None


def _crossing(objects: list[dict], inputs: list[dict], y: float, after: int,
              rising: bool) -> float | None:
    """Where (x) the simulated player's centre first crosses height `y` after a tick."""
    trace = _sim_run(objects, inputs, max_ticks=after + 400).trace
    pairs = list(zip(trace["tick"], trace["x"], trace["y"], strict=True))
    for (t0, x0, y0), (_t1, x1, y1) in zip(pairs, pairs[1:], strict=False):
        if t0 < after:
            continue
        if (y0 < y <= y1) if rising else (y0 > y >= y1):
            return x0 + (x1 - x0) * (y - y0) / (y1 - y0)
    return None


def _orb_span(base: list[dict], mini: bool) -> tuple[int, int] | None:
    """The ticks the player's box first and last overlaps an orb in column P18_COL."""
    half = 15.0 * (0.6 if mini else 1.0)
    x = cell(P18_COL, 0)[0]
    first = _sim_tick_at(base, [], x - 15 - half)
    last = _sim_tick_at(base, [], x + 15 + half)
    return (first, last) if first is not None and last is not None else None


async def probe_flight_contacts(r: Runner, out: dict) -> None:
    """P18: pads (A) and orbs (B) in the flight modes, full size and mini; a mini player
    gliding under a spike (C) or a block (D); the mini cube jumping a spike and onto a
    block (E); the wave into a wall, onto a block's top and under a block's bottom (F)."""
    runs: list[dict] = []
    raw: list[dict] = []
    ox, oy = cell(P18_COL, 0)
    px, py = cell(P18_COL, 0, PAD_Y_OFFSET)
    # A: each pad on the floor, no input.
    for mode in FLIGHT_MODES:
        for color, pad_id in PAD_IDS.items():
            for speed in P18_PAD_SPEEDS:
                objects = [*_p18_base(mode, speed=speed), {"id": pad_id, "x": px, "y": py}]
                await _record(r, runs, raw, f"A {mode} {color} pad {speed}", objects, [], 500,
                              part="A", mode=mode, color=color, speed=speed)
        objects = [*_p18_base(mode, mini=True), {"id": PAD_IDS["yellow"], "x": px, "y": py}]
        await _record(r, runs, raw, f"A mini {mode} yellow pad", objects, [], 500,
                      part="A", mode=mode, color="yellow", mini=True)
    # B: an orb on the floor, pressed at a sweep of ticks while the player passes it
    # (yellow), in the middle (pink, blue), held through it, and pressed just before.
    for mode in FLIGHT_MODES:
        for mini in (False, True):
            base = _p18_base(mode, mini=mini)
            span = _orb_span(base, mini)
            if span is None:
                print(f"    B {mode}: the simulator could not reach the orb; skipped")
                continue
            first, last = span
            middle = (first + last) // 2
            sweeps: list[tuple[str, list[dict], dict]] = []
            for color in ("yellow", "pink", "blue") if not mini else ("yellow",):
                ticks = range(first - 4, last + 5, 4) if color == "yellow" and not mini \
                    else (middle,)
                sweeps += [(color, press(t), {"press_tick": t}) for t in ticks]
            if not mini:
                sweeps += [("yellow", press(first - 6, hold=last - first + 12),
                            {"buffering": "held_through"}),
                           ("yellow", press(first - 4), {"buffering": "pressed_before"})]
            for color, inputs, meta in sweeps:
                objects = [*base, {"id": ORB_IDS[color], "x": ox, "y": oy}]
                label = (f"B {'mini ' if mini else ''}{mode} {color} orb "
                         + str(meta.get("press_tick", meta.get("buffering"))))
                await _record(r, runs, raw, label, objects, inputs, 500, part="B", mode=mode,
                              color=color, mini=mini, touch=[first, last], **meta)
    # C and D: mini, gliding on the floor, a spike or a block just above (as P17 C, D).
    sx = cell(P18_COL, 0)[0]
    for mode, clearances in P18_MINI_CLEARANCES.items():
        top = P18_MINI_TOP[mode]
        base = _p18_base(mode, mini=True)
        for d in clearances:
            spike = {"id": 8, "x": sx, "y": top + d + 6.0}      # hitbox 6 below centre
            await _record(r, runs, raw, f"C mini {mode} spike {d:+d}", [*base, spike], [],
                          400, part="C", mode=mode, mini=True, clearance=d)
        for d in P18_MINI_BLOCK_CLEARANCES:
            block = {"id": 1, "x": sx, "y": top + d + 15.0}
            await _record(r, runs, raw, f"D mini {mode} block {d:+d}", [*base, block], [],
                          400, part="D", mode=mode, mini=True, clearance=d)
    # E: the mini cube jumps a ground spike and onto a one-block step (as P5, P4).
    base = _p18_base("cube", mini=True)
    reach = _sim_tick_at(base, [], sx - 15 - 9)
    if reach is not None:
        # A mini jump is in the air about 80 ticks (1.3 units a tick at 1x).
        for kind, object_id in (("spike", 8), ("block", 1)):
            objects = [*base, {"id": object_id, "x": sx, "y": 15.0}]
            for t in range(reach - 96, reach + 5, 4):
                await _record(r, runs, raw, f"E mini cube {kind} press {t}", objects,
                              press(t), 400, part="E", mode="cube", mini=True, kind=kind,
                              press_tick=t)
    # F: the wave into a wall; falling onto a step whose left edge is at column 20; and
    # climbing under a ceiling starting there, crossing the face near the corner.
    wave = _p18_base("wave")
    wall = [{"id": 1, "x": sx, "y": cell(P18_COL, row)[1]} for row in range(3)]
    await _record(r, runs, raw, "F wave into a wall", [*wave, *wall], [], 400, part="F",
                  mode="wave", kind="wall")
    edge = 30.0 * 20
    step_top = [{"id": 1, "x": cell(c, 1)[0], "y": cell(c, 1)[1]} for c in range(20, 40)]
    ceiling = [{"id": 1, "x": cell(c, 4)[0], "y": cell(c, 4)[1]} for c in range(20, 40)]
    at_edge = _sim_tick_at(wave, [], edge) or 0
    for kind, blocks, y, rising in (("step", step_top, 60.0, False),
                                    ("ceiling", ceiling, 120.0, True)):
        found: list[tuple[float, list[dict]]] = []
        for t in range(P18_WAVE_CLIMB + 10, at_edge):
            inputs = (press(P18_WAVE_CLIMB, hold=t - P18_WAVE_CLIMB) if not rising
                      else press(t, hold=600))
            x = _crossing(wave, inputs, y, t, rising=rising)
            if x is not None and abs(x - edge) <= P18_CORNER_REACH + 3:
                found.append((x - edge, inputs))
        chosen = {min(found, key=lambda f, d=d: abs(f[0] - d))[0]: None
                  for d in P18_CORNER_OFFSETS} if found else {}
        for offset, inputs in found:
            if offset in chosen:
                await _record(r, runs, raw, f"F wave {kind}, crossing {offset:+.0f}",
                              [*wave, *blocks], inputs, at_edge + 300, part="F",
                              mode="wave", kind=kind, offset=round(offset, 1))
    out["P18_flight"] = [{k: v for k, v in run.items() if k != "trace"} for run in runs]
    save_fixture("cal_flight", "P18 flight orbs and pads, mini contacts, the wave in solids",
                 runs)
    save_raw("P18", raw)


# P19 (M5, Sim-3): the robot, explored as P15 explored the flight modes. Nothing is
# fitted here; the traces are read offline. The robot's jump grows with how long the
# press is held, up to a limit, so A sweeps the hold; B repeats a few holds mini, upside
# down, at each speed and from a high portal; C taps in a rhythm and holds through
# landings; D and E meet pads and orbs; F jumps a spike and onto a one-block step. Where
# a press must land on an object, the simulator aims it with the cube, which moves
# across at the same speed.

ROBOT_PORTAL = 745
P19_COL = 14                                   # pads, orbs, spike and step
P19_HOLDS = (1, 3, 6, 10, 15, 20, 25, 30, 40, 50, 80)
P19_SHORT_HOLDS = (1, 10, 25, 80)
P19_SPEED_HOLDS = (1, 25, 80)
#: Ticks of travel between a robot press and the spike or step, at 1x: the cube's jump
#: is in the air ~104 ticks, so these straddle where a jump of each hold clears it.
P19_LEADS = {1: (20, 35, 50), 10: (25, 45, 65), 25: (30, 55, 80)}


def _p19_base(speed: str = "1x", mini: bool = False, flipped: bool = False,
              high: bool = False) -> list[dict[str, Any]]:
    """As P15: a speed portal (column 2), mini (4), the robot portal (6), gravity (9)."""
    objects: list[dict[str, Any]] = []
    if speed != "1x":
        x, y = cell(2, 1)
        objects.append({"id": dict(SPEED_PORTALS)[speed], "x": x, "y": y})
    if mini:
        x, y = cell(4, 1)
        objects.append({"id": SIZE_MINI, "x": x, "y": y})
    if high:
        x, y = cell(3, 0, PAD_Y_OFFSET)
        objects.append({"id": 35, "x": x, "y": y})
        objects.append({"id": ROBOT_PORTAL, "x": cell(6, 0)[0], "y": _high_portal_y()})
    else:
        x, y = cell(6, 1)
        objects.append({"id": ROBOT_PORTAL, "x": x, "y": y})
    if flipped:
        x, y = cell(9, 1)
        objects.append({"id": GRAVITY_FLIP, "x": x, "y": y})
    return objects


def _cube_only(objects: list[dict]) -> list[dict]:
    """The layout without its robot portal, for aiming with the cube simulator."""
    return [o for o in objects if o["id"] != ROBOT_PORTAL]


def p19_plan() -> list[tuple[str, list[dict], list[dict], int, dict]]:
    """Every P19 run as (label, objects, inputs, max_ticks, meta). It needs no game, so
    the layouts and aims can be checked offline."""
    runs: list[tuple[str, list[dict], list[dict], int, dict]] = []
    base = _p19_base()
    start = P15_START
    # A: one press, held for each length.
    for hold in P19_HOLDS:
        runs.append((f"A hold {hold}", base, press(start, hold=hold), 700,
                     {"part": "A", "hold": hold}))
    # B: a few holds mini, upside down and at each speed; from a high portal, idle and
    # pressed while falling.
    variants: list[tuple[str, dict[str, Any], tuple[int, ...]]] = [
        ("mini", {"mini": True}, P19_SHORT_HOLDS),
        ("flipped", {"flipped": True}, P19_SHORT_HOLDS),
        *[(f"speed {sp}", {"speed": sp}, P19_SPEED_HOLDS) for sp in ("0.5x", "2x", "3x", "4x")],
    ]
    for label, kwargs, holds in variants:
        objects = _p19_base(**kwargs)
        for hold in holds:
            runs.append((f"B {label} hold {hold}", objects, press(start, hold=hold), 900,
                         {"part": "B", "setup": label, "hold": hold, **kwargs}))
    runs.append(("B flipped idle", _p19_base(flipped=True), [], 600,
                 {"part": "B", "setup": "flipped", "flipped": True}))
    high = _p19_base(high=True)
    runs.append(("B high idle", high, [], 700, {"part": "B", "setup": "high", "high": True}))
    after = _sim_tick_at(_cube_only(high), [], cell(6, 0)[0] + 30)
    if after is not None:
        runs.append((f"B high press {after} falling", high, press(after, hold=25), 700,
                     {"part": "B", "setup": "high", "high": True, "press_tick": after,
                      "hold": 25}))
    # C: taps in a rhythm, short and long; held for a long time (does it jump again on
    # landing?); a press released and pressed again in the air.
    runs.append(("C taps 1 x5", base, [i for k in range(5) for i in press(start + 70 * k)],
                 1000, {"part": "C", "kind": "taps", "hold": 1}))
    runs.append(("C taps 20 x5", base,
                 [i for k in range(5) for i in press(start + 90 * k, hold=20)], 1100,
                 {"part": "C", "kind": "taps", "hold": 20}))
    runs.append(("C hold 700", base, press(start, hold=700), 1200,
                 {"part": "C", "kind": "hold_long", "hold": 700}))
    runs.append(("C repress in the air", base,
                 [*press(start, hold=8), *press(start + 20, hold=30)], 800,
                 {"part": "C", "kind": "repress"}))
    # D: each pad on the floor, no input; the yellow pad mini.
    px, py = cell(P19_COL, 0, PAD_Y_OFFSET)
    for color, pad_id in PAD_IDS.items():
        runs.append((f"D {color} pad", [*base, {"id": pad_id, "x": px, "y": py}], [], 600,
                     {"part": "D", "color": color}))
    mini = _p19_base(mini=True)
    runs.append(("D mini yellow pad", [*mini, {"id": PAD_IDS["yellow"], "x": px, "y": py}],
                 [], 600, {"part": "D", "color": "yellow", "mini": True}))
    # E: an orb on the floor, pressed while the robot overlaps it: tapped and held.
    ox, oy = cell(P19_COL, 0)
    span = _orb_span(_cube_only(base), mini=False)
    if span is not None:
        first, last = span
        middle = (first + last) // 2
        for color, orb_id in ORB_IDS.items():
            for hold in ((1, 25) if color == "yellow" else (1,)):
                runs.append((f"E {color} orb hold {hold}",
                             [*base, {"id": orb_id, "x": ox, "y": oy}],
                             press(middle, hold=hold), 600,
                             {"part": "E", "color": color, "hold": hold,
                              "press_tick": middle, "touch": [first, last]}))
        yellow = {"id": ORB_IDS["yellow"], "x": ox, "y": oy}
        runs.append(("E yellow orb held through", [*base, yellow],
                     press(first - 6, hold=last - first + 12), 600,
                     {"part": "E", "color": "yellow", "buffering": "held_through",
                      "touch": [first, last]}))
    # F: a spike, and a one-block step from column 14 on, jumped with three holds at
    # three leads each.
    fx = cell(P19_COL, 0)[0]
    reach = _sim_tick_at(_cube_only(base), [], fx - 15 - HALF_SIM)
    if reach is not None:
        step = [{"id": 1, "x": cell(c, 0)[0], "y": 15.0}
                for c in range(P19_COL, P19_COL + 12)]
        for kind, objects in (("spike", [*base, {"id": 8, "x": fx, "y": 15.0}]),
                              ("step", [*base, *step])):
            for hold, leads in P19_LEADS.items():
                for lead in leads:
                    t = reach - lead
                    runs.append((f"F {kind} hold {hold} lead {lead}", objects,
                                 press(t, hold=hold), 700,
                                 {"part": "F", "kind": kind, "hold": hold, "lead": lead,
                                  "press_tick": t}))
    return runs


async def probe_robot(r: Runner, out: dict) -> None:
    """P19: the robot (Sim-3 exploration); see `p19_plan`."""
    runs: list[dict] = []
    raw: list[dict] = []
    try:
        for label, objects, inputs, max_ticks, meta in p19_plan():
            await _record(r, runs, raw, label, objects, inputs, max_ticks, mode="robot",
                          **meta)
    finally:
        # Saved even when a run fails part-way: five minutes of runs are not thrown
        # away for the last one (2026-11-24: a crash on run 12 lost the first 11).
        out["P19_robot"] = [{k: v for k, v in run.items() if k != "trace"} for run in runs]
        if runs:
            save_fixture("cal_robot",
                         "P19 the robot: holds, sizes, speeds, pads, orbs, spike, step", runs)
            save_raw("P19", raw)


# P20 (M5, Sim-3): what P19 left unverifiable or untested about the robot, each run aimed
# by the simulator, which plays the robot upright since P19. A: upside down under a roof
# (P19's flipped runs had none, so the robot fell off the top of the level). B: how early
# a press made in the air may come and still jump at landing. C: a two-block step's
# face, swept across where the simulator turns a landing into a death. D: a block above
# the floor, met from below. E: the lowest jump that lands on a one-block step.

P20_ROOF = [(c, 7) for c in range(4, 40)]        # as P16's, inside the cleared columns
P20_FLIPPED_PRESS = 420                          # well after the robot reaches the roof
P20_FLIPPED_HOLDS = (1, 10, 25, 68)
#: B: a 25-tick jump from the ground at P15_START; the second press comes this many ticks
#: before its landing and is held through it.
P20_BUFFER_LEADS = (1, 2, 3, 4, 6, 8, 10, 14, 18, 24, 32, 40)
P20_BUFFER_HOLD = 40
#: C: the holds swept against the step's face, and how far either side of the edge.
P20_FACE_HOLDS = (30, 38, 68)
P20_FACE_REACH = 6
#: D: (block row, holds) with the head reaching the block's bottom inside the sweep.
P20_CEILINGS = ((2, (6, 8, 10, 12, 14, 16)), (3, (30, 32, 34, 36, 38, 40)))
#: E: the shortest holds onto a one-block step (P19 F: 1 died, 10 landed; the simulator
#: lands from 2), at three leads.
P20_STEP_HOLDS = (1, 2, 3, 4, 5, 6)
P20_STEP_LEADS = (25, 35, 45)


def _blocks(cells: list[tuple[int, int]]) -> list[dict[str, Any]]:
    return [{"id": 1, "x": cell(c, r)[0], "y": cell(c, r)[1]} for c, r in cells]


def _robot_outcome(objects: list[dict], inputs: list[dict]) -> str:
    return _sim_run(objects, inputs, 900).outcome


def _edges(objects: list[dict], hold: int, leads: range, reach: int) -> list[int]:
    """Leads where the simulator's outcome changes between neighbouring leads."""
    outcomes = [(lead, _robot_outcome(objects, press(reach - lead, hold=hold)))
                for lead in leads]
    return [b for (_, x), (b, y) in zip(outcomes, outcomes[1:], strict=False) if x != y]


def p20_plan() -> list[tuple[str, list[dict], list[dict], int, dict]]:
    """Every P20 run as (label, objects, inputs, max_ticks, meta); no game needed."""
    runs: list[tuple[str, list[dict], list[dict], int, dict]] = []
    base = _p19_base()
    roof = _blocks(P20_ROOF)
    # A: upside down under a roof: idle, a press of each length, mini; a blue pad and a
    # blue orb on the floor send it up instead.
    flipped = [*_p19_base(flipped=True), *roof]
    runs.append(("A flipped idle", flipped, [], 800, {"part": "A", "flipped": True}))
    for hold in P20_FLIPPED_HOLDS:
        runs.append((f"A flipped hold {hold}", flipped, press(P20_FLIPPED_PRESS, hold=hold),
                     900, {"part": "A", "flipped": True, "hold": hold}))
    mini = [*_p19_base(mini=True, flipped=True), *roof]
    runs.append(("A mini flipped hold 25", mini, press(P20_FLIPPED_PRESS, hold=25), 900,
                 {"part": "A", "flipped": True, "mini": True, "hold": 25}))
    px, py = cell(P19_COL, 0, PAD_Y_OFFSET)
    blue_pad = [*base, *roof, {"id": PAD_IDS["blue"], "x": px, "y": py}]
    runs.append(("A blue pad to the roof", blue_pad, [], 800, {"part": "A", "color": "blue"}))
    runs.append(("A blue pad, then hold 25", blue_pad, press(P20_FLIPPED_PRESS + 40, hold=25),
                 900, {"part": "A", "color": "blue", "hold": 25}))
    span = _orb_span(_cube_only(base), mini=False)
    if span is not None:
        ox, oy = cell(P19_COL, 0)
        orb = [*base, *roof, {"id": ORB_IDS["blue"], "x": ox, "y": oy}]
        middle = (span[0] + span[1]) // 2
        runs.append(("A blue orb to the roof", orb, press(middle), 800,
                     {"part": "A", "color": "blue", "press_tick": middle}))
    # B: a press in the air, held through the landing of a 25-tick jump.
    first = press(P15_START, hold=25)
    trace = _sim_run(base, first, 700).trace
    landing = next(t for t, g in zip(trace["tick"], trace["grounded"], strict=True)
                   if t > P15_START + 2 and g)
    for lead in P20_BUFFER_LEADS:
        second = press(landing - lead, hold=P20_BUFFER_HOLD)
        runs.append((f"B air press {lead} before landing", base, [*first, *second], 800,
                     {"part": "B", "lead": lead, "landing": landing}))
    # C: a two-block step from column 16, jumped at leads either side of each edge the
    # simulator finds between landing on it and dying on its face.
    step = _blocks([(c, r) for c in range(16, 28) for r in range(2)])
    face = [*base, *step]
    reach = _sim_tick_at(base, [], cell(16, 0)[0] - 15 - HALF_SIM)
    if reach is not None:
        for hold in P20_FACE_HOLDS:
            leads: set[int] = set()
            for edge in _edges(face, hold, range(10, 100, 2), reach):
                leads |= set(range(edge - P20_FACE_REACH, edge + P20_FACE_REACH + 1, 2))
            for lead in sorted(leads):
                runs.append((f"C face hold {hold} lead {lead}", face,
                             press(reach - lead, hold=hold), 700,
                             {"part": "C", "hold": hold, "lead": lead, "press_tick": reach - lead}))
    # D: a row of blocks above the floor, the robot jumping into it from below.
    under = _sim_tick_at(base, [], cell(15, 0)[0])
    if under is not None:
        for row, holds in P20_CEILINGS:
            ceiling = [*base, *_blocks([(c, row) for c in range(14, 20)])]
            for hold in holds:
                runs.append((f"D ceiling row {row} hold {hold}", ceiling,
                             press(under, hold=hold), 700,
                             {"part": "D", "row": row, "hold": hold, "press_tick": under}))
    # E: short holds onto a one-block step.
    one = [*base, *_blocks([(c, 0) for c in range(P19_COL, P19_COL + 12)])]
    reach_one = _sim_tick_at(base, [], cell(P19_COL, 0)[0] - 15 - HALF_SIM)
    if reach_one is not None:
        for hold in P20_STEP_HOLDS:
            for lead in P20_STEP_LEADS:
                t = reach_one - lead
                runs.append((f"E step hold {hold} lead {lead}", one, press(t, hold=hold), 700,
                             {"part": "E", "hold": hold, "lead": lead, "press_tick": t}))
    return runs


async def probe_robot_contacts(r: Runner, out: dict) -> None:
    """P20: the robot upside down, buffered presses, faces, ceilings; see `p20_plan`."""
    runs: list[dict] = []
    raw: list[dict] = []
    try:
        for label, objects, inputs, max_ticks, meta in p20_plan():
            await _record(r, runs, raw, label, objects, inputs, max_ticks, mode="robot",
                          **meta)
    finally:
        out["P20_robot"] = [{k: v for k, v in run.items() if k != "trace"} for run in runs]
        if runs:
            save_fixture("cal_robot_contacts",
                         "P20 the robot upside down, buffered presses, faces, ceilings",
                         runs)
            save_raw("P20", raw)


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
    if any(o.x < CLEAR_X or FAR_X[0] <= o.x < FAR_X[1] for o in model):
        print(f"\nThe first {CLEAR_X // 30} columns, or columns {FAR_COL}-{FAR_COL + 40}, "
              "hold objects. Open an empty level.")
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
        ("P10", "landing near a block's edge", lambda: probe_landing_edge(r, out)),
        ("P11", "what makes the stair landing step further",
         lambda: probe_landing_cause(r, out)),
        ("P12", "jump snapping by speed and spacing", lambda: probe_jump_snap(r, out)),
        ("P13", "orbs and pads at every speed",
         lambda: probe_orbs_pads_by_speed(r, out, cube)),
        ("P14", "the timeline at five speed portals", lambda: probe_timeline(r, out)),
        ("P15", "ship, ball, ufo and wave (Sim-2 exploration)", lambda: probe_modes(r, out)),
        ("P16", "upside down, blue pad and orb, mini (Sim-2 exploration)",
         lambda: probe_gravity(r, out)),
        ("P17", "back to the cube, mode bands, spikes and blocks (Sim-2)",
         lambda: probe_contacts(r, out)),
        ("P18", "flight orbs and pads, mini contacts, the wave in solids (Sim-2)",
         lambda: probe_flight_contacts(r, out)),
        ("P19", "the robot (Sim-3 exploration)", lambda: probe_robot(r, out)),
        ("P20", "the robot upside down, buffered, faces, ceilings (Sim-3)",
         lambda: probe_robot_contacts(r, out)),
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
