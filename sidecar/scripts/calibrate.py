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
