"""The simulator against every replay recorded in the game (LEVEL-DESIGN-SPEC.md 6.10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import Cube, CubeParams, simulate
from gd_ai_assistant.design.sim.world import World, world_from_objects

FIXTURES = Path(__file__).parent / "fixtures" / "replays"
ROLES = load_role_table()
PARAMS = CubeParams.from_constants(load_constants())

#: LEVEL-DESIGN-SPEC.md 6.10 tolerances.
MAX_DX = 0.5
MAX_DY = 1.0


def _cases():
    for path in sorted(FIXTURES.glob("cal_*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for index, run in enumerate(fixture["runs"]):
            if "error" in run:
                continue
            objects = (run.get("objects") or fixture["objects"]
                       or [run[k] for k in ("portal", "pad") if k in run])
            label = run.get("press_tick", run.get("speed", run.get("buffering", index)))
            yield pytest.param(objects, run, id=f"{path.stem}-{index}-{label}")


@pytest.mark.parametrize(("objects", "run"), list(_cases()))
def test_golden_replay(objects, run) -> None:
    world = world_from_objects(objects, ROLES)
    # Two extra ticks: the game numbers a death caught mid-step by completed ticks.
    if "start" in run:
        # From a start position: already moving at sim tick 1, one tick ahead of the game
        # (the same lead as from the level start, playtest 2026-11-09).
        start = Cube(x=run["start"]["x"], y=run["start"]["y"], grounded=True)
        inputs = [{**i, "tick": i["tick"] - 1} for i in run["inputs"]]
        # Sim tick = game tick - 1: a run the game stopped alive at its last tick ends there.
        limit = run["ticks"] + 1 if run["outcome"] == "death" else run["ticks"] - 1
        result = simulate(world, PARAMS, inputs, max_ticks=limit, start=start)
        lead = 1
    else:
        limit = run["ticks"] + 2 if run["outcome"] == "death" else run["ticks"]
        result = simulate(world, PARAMS, run["inputs"], max_ticks=limit)
        lead = 0
    simulated = {t + lead: xy for t, xy in zip(
        result.trace["tick"], zip(result.trace["x"], result.trace["y"], strict=True),
        strict=True)}
    trace = run["trace"]
    died = run["outcome"] == "death"
    last = trace["tick"][-1]
    for tick, x, y in zip(trace["tick"], trace["x"], trace["y"], strict=True):
        if died and tick == last:
            continue  # the death sample is labelled one tick apart; compared below
        if tick not in simulated:
            continue  # tick 0 from a start position: before the simulator's first sample
        sx, sy = simulated[tick]
        assert abs(sx - x) <= MAX_DX and abs(sy - y) <= MAX_DY, (tick, (x, y), (sx, sy))
    if died:
        assert result.outcome == "death"
        # A shallow side hit resolves on the same tick or one tick later in the game, never
        # earlier; identical runs differ (P11, B runs 29-45), so one tick of x is allowed.
        late = run["death"]["x"] - result.death.x
        assert -MAX_DX <= late <= PARAMS.dx["1x"] + MAX_DX, late
        # One tick late in the air also moves y by up to one tick of the fall.
        dy = MAX_DY if late <= MAX_DX else MAX_DY + PARAMS.terminal_velocity * PARAMS.scale
        assert abs(result.death.y - run["death"]["y"]) <= dy
    else:
        assert result.outcome != "death", result.death


@pytest.mark.parametrize("speed", ["0.5x", "1x", "2x", "3x", "4x"])
def test_flat_floor_needs_no_input(speed) -> None:
    """6.10 property: a flat floor with nothing on it is survived without a press."""
    from gd_ai_assistant.design.sim.cube import Cube

    result = simulate(World([], []), PARAMS, [], max_ticks=2400, start=Cube(speed=speed),
                      start_tick=1)
    assert result.outcome == "max_ticks"
    assert result.final.y == 15.0


def test_constants_are_calibrated() -> None:
    assert PARAMS.calibrated


def test_orb_and_pad_impulses_by_speed() -> None:
    """P13 values per speed win; a speed without one falls back to the 1x impulse."""
    from dataclasses import replace

    params = replace(PARAMS, orb_by_speed={"2x": {"yellow": 20.0}},
                     pad_by_speed={"0.5x": {"pink": 9.0}})
    assert params.orb_for("2x", "yellow") == 20.0
    assert params.orb_for("2x", "pink") == PARAMS.orb_impulse["pink"]
    assert params.orb_for("3x", "yellow") == PARAMS.orb_impulse["yellow"]
    assert params.pad_for("0.5x", "pink") == 9.0
    assert params.pad_for("1x", "pink") == PARAMS.pad_impulse["pink"]
