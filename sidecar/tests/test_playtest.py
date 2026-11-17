"""`playtest_verify` against a fake game built from the simulator (LEVEL-DESIGN-SPEC.md 7.3).

The fake plays a replay the way the mod does: tick 0 is the state before the first
tick, the first `lead` ticks stand still, and an input at game tick N acts on the move of
tick N+1. Its physics can be made to differ from the simulator's, to check that a route
the game does not survive is reported, not confirmed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.level_model import LevelModel, LevelObject
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import Cube, CubeParams, advance
from gd_ai_assistant.design.sim.playtest import LEAD, TRACE_COLUMNS, playtest_verify
from gd_ai_assistant.design.sim.world import world_from_objects

ROLES = load_role_table()
PARAMS = CubeParams.from_constants(load_constants())


@dataclass
class Response:
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class FakeGame:
    def __init__(self, objects: list[dict[str, Any]], *, params: CubeParams = PARAMS,
                 lead: int = LEAD, start_offset: float = 0.0,
                 fail: dict[str, Any] | None = None) -> None:
        self.world = world_from_objects(objects, ROLES)
        self.params = params
        self.lead = lead
        self.start_offset = start_offset
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.objects = objects

    async def __call__(self, type_name: str, payload: dict[str, Any],
                       timeout: float | None = None) -> Response:
        assert type_name == "run_replay"
        self.calls.append(payload)
        if self.fail:
            return Response(False, error=self.fail)
        start = payload["start"]
        cube = Cube()
        if start["kind"] == "start_pos":
            obj = next(o for o in self.objects if o.get("uid") == start["uid"])
            cube = Cube(x=obj["x"] + self.start_offset, y=obj["y"])
        by_tick: dict[int, list[str]] = {}
        for item in payload["inputs"]:
            by_tick.setdefault(item["tick"], []).append(item["action"])
        trace: dict[str, list[Any]] = {"tick": [], "x": [], "y": []}

        def sample(t: int) -> None:
            trace["tick"].append(t)
            trace["x"].append(cube.x)
            trace["y"].append(cube.y)

        for t in range(self.lead + 1):
            sample(t)
        outcome, death = "max_ticks", None
        for t in range(self.lead + 1, payload["max_ticks"] + 1):
            died = advance(cube, self.world, self.params, t, by_tick.get(t - 1, ()))
            sample(t)
            if died is not None:
                outcome = "death"
                death = {"tick": t, "x": died.x, "y": died.y}
                break
        return Response(True, {"outcome": outcome, "ticks": trace["tick"][-1],
                               "death": death, "trace": trace})


def _model(objects: list[dict[str, Any]]) -> LevelModel:
    records = [{"object_id": o.get("uid", i + 1000), "id": o["id"],
                "properties": {"x": o["x"], "y": o["y"]}, "save": ""}
               for i, o in enumerate(objects)]
    return LevelModel([LevelObject.from_record(r, ROLES) for r in records], revision=1)


def _run(game: FakeGame, objects, args) -> dict[str, Any]:
    return asyncio.run(playtest_verify(game, _model(objects), args))


SPIKE = [{"id": 8, "x": 255, "y": 15}]


def test_a_route_the_game_survives_is_confirmed() -> None:
    game = FakeGame(SPIKE)
    result = _run(game, SPIKE, {"x_range": [0, 600], "start": "level_start"})
    assert result["result"] == "confirmed", result
    assert result["comparison"]["agrees"]
    (press,) = result["route"]
    assert press["game_tick"] == press["tick"] + LEAD
    assert game.calls[0]["inputs"][0] == {"tick": press["game_tick"], "action": "press"}
    assert result["replays"] == 1


def test_edges_are_replayed_and_agree() -> None:
    pit = [{"id": 8, "x": 255 + 30 * c, "y": 15} for c in range(3)]
    game = FakeGame(pit)
    result = _run(game, pit, {"x_range": [0, 600], "start": "level_start", "edges": True})
    assert result["result"] == "confirmed"
    assert result["edges_agree"], result["edges"]
    played = [c for c in result["edges"][0]["checks"] if not c.get("skipped")]
    assert {c["expected"] for c in played} == {"survive", "die"}


def test_a_game_that_disagrees_fails_the_route() -> None:
    """Weaker jump in the 'game': the simulator's route over 3 spikes dies there."""
    weak = replace(PARAMS, jump_velocity=PARAMS.jump_velocity * 0.8)
    pit = [{"id": 8, "x": 255 + 30 * c, "y": 15} for c in range(3)]
    result = _run(FakeGame(pit, params=weak), pit, {"x_range": [0, 600],
                                                     "start": "level_start"})
    assert result["result"] == "failed_in_game"
    assert result["game"]["outcome"] == "death"
    assert not result["comparison"]["agrees"]


def test_a_start_position_with_another_lead_is_corrected() -> None:
    objects = [{"uid": 7, "id": 31, "x": 3075, "y": 15},
               {"id": 8, "x": 3615, "y": 15}]
    game = FakeGame(objects, lead=3)
    result = _run(game, objects, {"x_range": [3060, 3900]})
    assert result["start"]["kind"] == "start_pos" and result["start"]["uid"] == 7
    assert result["result"] == "confirmed", result
    assert any("not 1" in c for c in result["corrections"])
    assert result["replays"] == 2


def test_a_start_that_is_not_where_assumed_is_solved_again() -> None:
    objects = [{"uid": 7, "id": 31, "x": 3075, "y": 15},
               {"id": 8, "x": 3615, "y": 15}]
    game = FakeGame(objects, start_offset=-15.0)
    result = _run(game, objects, {"x_range": [3060, 3900]})
    assert result["result"] == "confirmed", result
    assert any("solved again" in c for c in result["corrections"])


def test_an_impossible_section_is_not_played() -> None:
    wall = [{"id": 1, "x": 255, "y": 15 + 30 * row} for row in range(5)]
    game = FakeGame(wall)
    result = _run(game, wall, {"x_range": [0, 600], "start": "level_start"})
    assert result["result"] == "not_played_impossible"
    assert game.calls == []


def test_a_cancelled_replay_is_reported() -> None:
    game = FakeGame(SPIKE, fail={"code": "CANCELLED",
                                 "message": "the user pressed a button during the replay"})
    result = _run(game, SPIKE, {"x_range": [0, 600], "start": "level_start"})
    assert result["result"] == "error" and result["code"] == "CANCELLED"


def test_presses_are_played_in_the_middle_of_their_windows() -> None:
    """The search leaves each press on its window's last tick; the replay centres it."""
    pit = [{"id": 8, "x": 255 + 30 * c, "y": 15} for c in range(3)]
    result = _run(FakeGame(pit), pit, {"x_range": [0, 600], "start": "level_start"})
    assert result["result"] == "confirmed"
    for press in result["route"]:
        lo, hi = press["window_ticks"]
        assert hi - lo >= 2, press
        assert lo < press["tick"] < hi, press
        assert abs(press["tick"] - (lo + hi) / 2) <= 1, press


def test_traces_are_kept_when_the_game_disagrees() -> None:
    weak = replace(PARAMS, jump_velocity=PARAMS.jump_velocity * 0.8)
    pit = [{"id": 8, "x": 255 + 30 * c, "y": 15} for c in range(3)]
    args = {"x_range": [0, 600], "start": "level_start"}
    without = _run(FakeGame(pit, params=weak), pit, args)
    assert "trace_rows" not in without["comparison"]
    kept = _run(FakeGame(pit, params=weak), pit, {**args, "traces": True})
    rows = kept["comparison"]["trace_rows"]
    assert all(len(r) == len(TRACE_COLUMNS) for r in rows)
    assert rows[-1][0] == kept["game"]["death"]["tick"]
    assert any(r[6] is not None for r in rows)


def test_traces_are_not_kept_when_the_game_agrees() -> None:
    result = _run(FakeGame(SPIKE), SPIKE, {"x_range": [0, 600], "start": "level_start",
                                           "traces": True})
    assert result["comparison"]["agrees"]
    assert "trace_rows" not in result["comparison"]


def test_a_ship_hold_is_played_and_confirmed() -> None:
    objects = [{"id": 13, "x": 255, "y": 45}, {"id": 8, "x": 615, "y": 15}]
    game = FakeGame(objects)
    result = _run(game, objects, {"x_range": [0, 900], "start": "level_start",
                                  "edges": True})
    assert result["result"] == "confirmed", result
    (hold,) = result["route"]
    assert hold["action"] == "hold"
    assert hold["game_release_tick"] == hold["release_tick"] + LEAD
    played = game.calls[0]["inputs"]
    assert {"tick": hold["game_tick"], "action": "press"} in played
    assert {"tick": hold["game_release_tick"], "action": "release"} in played
    assert result["edges_agree"], result["edges"]
