"""`simulate_inputs` (LEVEL-DESIGN-SPEC.md 6.7): a fixed script, played in the simulator."""

from __future__ import annotations

from test_design_agent import ScriptProvider, call, make_loop, text
from test_grid_agent import _tool_result

from evals.fake_editor import FakeEditor
from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import CubeParams, simulate
from gd_ai_assistant.design.sim.inputs import MAX_ROWS, run_inputs
from gd_ai_assistant.design.sim.world import world_from_objects

PARAMS = CubeParams.from_constants(load_constants())
SPIKE = world_from_objects([{"id": 8, "x": 255, "y": 15}], load_role_table())


def _tap(tick: int) -> list[dict]:
    return [{"tick": tick, "action": "press"}, {"tick": tick + 1, "action": "release"}]


def test_no_input_dies_on_the_spike() -> None:
    result = run_inputs(SPIKE, PARAMS, {"until_x": 600})
    assert result["outcome"] == "death"
    assert result["death"]["cause"] == "hazard" and result["death"]["x"] < 255


def test_ticks_play_exactly_as_simulate() -> None:
    """P5: a press at replay tick 146 clears the spike in the game."""
    result = run_inputs(SPIKE, PARAMS, {"until_x": 600, "inputs": _tap(146),
                                        "sample_every": 1})
    expected = simulate(SPIKE, PARAMS, _tap(146), max_ticks=10_000, until_x=600)
    assert result["outcome"] == expected.outcome == "reached"
    assert result["ticks"] == expected.ticks
    ys = dict(zip(expected.trace["tick"], expected.trace["y"], strict=True))
    assert len(result["trace"]["tick"]) > 200
    for tick, y in zip(result["trace"]["tick"], result["trace"]["y"], strict=True):
        assert y == round(ys[tick], 3), tick
    kinds = [e["event"] for e in result["events"]]
    assert kinds[:3] == ["press", "takeoff", "release"] and "landing" in kinds


def test_an_input_by_x_lands_on_the_first_tick_at_that_x() -> None:
    by_tick = run_inputs(SPIKE, PARAMS, {"until_x": 600, "inputs": _tap(146)})
    trace = simulate(SPIKE, PARAMS, [], max_ticks=146, until_x=None).trace
    x = trace["x"][trace["tick"].index(146)] - 0.01       # reached first on tick 146
    by_x = run_inputs(SPIKE, PARAMS, {"until_x": 600, "inputs": [
        {"x": x, "action": "press"}, {"tick": 147, "action": "release"}]})
    assert by_x["inputs_by_x"] == [{"x": x, "action": "press", "tick": 146}]
    assert by_x["outcome"] == "reached" and by_x["trace"] == by_tick["trace"]


def test_an_explicit_start_and_inputs_left_unplayed() -> None:
    result = run_inputs(SPIKE, PARAMS, {
        "until_x": 600, "start_from": "explicit", "start": {"x": 300, "speed": "2x"},
        "inputs": [{"x": 900, "action": "press"}]})
    assert result["outcome"] == "reached" and result["start"]["x"] == 300
    assert result["final"]["speed"] == "2x"
    assert result["not_played"] == [{"x": 900.0, "action": "press"}]


def test_bad_scripts_are_refused() -> None:
    assert "until_x" in run_inputs(SPIKE, PARAMS, {})["error"]
    both = {"until_x": 600, "inputs": [{"tick": 1, "x": 30, "action": "press"}]}
    assert "exactly one" in run_inputs(SPIKE, PARAMS, both)["error"]
    hop = {"until_x": 600, "inputs": [{"tick": 1, "action": "hop"}]}
    assert "press or release" in run_inputs(SPIKE, PARAMS, hop)["error"]


def test_a_long_run_keeps_the_trace_short() -> None:
    result = run_inputs(world_from_objects([], load_role_table()), PARAMS,
                        {"until_x": 30_000, "sample_every": 1})
    assert result["outcome"] == "reached"
    assert len(result["trace"]["tick"]) <= MAX_ROWS + 2
    assert result["sample_every"] > 1


async def test_simulate_inputs_through_the_agent() -> None:
    editor = FakeEditor()
    editor.add(8, 255, 15)
    provider = ScriptProvider([call("simulate_inputs", {"until_x": 600}), text()])
    await make_loop(editor, provider).run(turn_id="t1", message_id="m1", user_text="why?")
    result = _tool_result(provider)
    assert result["outcome"] == "death" and result["death"]["cause_uid"] is not None
