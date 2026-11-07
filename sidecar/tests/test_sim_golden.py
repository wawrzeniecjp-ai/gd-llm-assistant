"""The simulator against every replay recorded in the game (LEVEL-DESIGN-SPEC.md 6.10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import CubeParams, simulate
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
            objects = fixture["objects"] or [run[k] for k in ("portal", "pad") if k in run]
            label = run.get("press_tick", run.get("speed", run.get("buffering", index)))
            yield pytest.param(objects, run, id=f"{path.stem}-{label}")


@pytest.mark.parametrize(("objects", "run"), list(_cases()))
def test_golden_replay(objects, run) -> None:
    world = world_from_objects(objects, ROLES)
    # Two extra ticks: the game numbers a death caught mid-step by completed ticks.
    result = simulate(world, PARAMS, run["inputs"], max_ticks=run["ticks"] + 2)
    simulated = dict(zip(result.trace["tick"],
                         zip(result.trace["x"], result.trace["y"], strict=True), strict=True))
    trace = run["trace"]
    died = run["outcome"] == "death"
    last = trace["tick"][-1]
    for tick, x, y in zip(trace["tick"], trace["x"], trace["y"], strict=True):
        if died and tick == last:
            continue  # the death sample is labelled one tick apart; compared below
        sx, sy = simulated[tick]
        assert abs(sx - x) <= MAX_DX and abs(sy - y) <= MAX_DY, (tick, (x, y), (sx, sy))
    if died:
        assert result.outcome == "death"
        assert abs(result.death.x - run["death"]["x"]) <= MAX_DX
        assert abs(result.death.y - run["death"]["y"]) <= MAX_DY
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
