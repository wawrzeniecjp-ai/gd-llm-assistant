"""Route search and timing windows (LEVEL-DESIGN-SPEC.md 6.8)."""

from __future__ import annotations

from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import Cube, CubeParams
from gd_ai_assistant.design.sim.solver import describe_route, solve, windows
from gd_ai_assistant.design.sim.world import World, world_from_objects

ROLES = load_role_table()
PARAMS = CubeParams.from_constants(load_constants())


def _world(objects: list[dict]) -> World:
    return world_from_objects(objects, ROLES)


def test_flat_floor_is_possible_with_no_input() -> None:
    solved = solve(World([], []), PARAMS, Cube(), 1200)
    assert solved.result == "possible" and solved.route == []


def test_one_spike_needs_one_press() -> None:
    world = _world([{"id": 8, "x": 255, "y": 15}])
    solved = solve(world, PARAMS, Cube(), 600)
    assert solved.result == "possible" and len(solved.route) == 1


def test_spike_window_matches_the_game() -> None:
    """P5 in the game: presses 119..173 cleared the spike, 116 and 176 died.

    The window search reaches 24 ticks either side of the route's press (6.8.3), so it
    must stop at the game's edge where it can see one, and never pass it.
    """
    from gd_ai_assistant.design.sim.solver import WINDOW_REACH

    world = _world([{"id": 8, "x": 255, "y": 15}])
    solved = solve(world, PARAMS, Cube(), 600)
    (press,) = solved.route
    (lo, hi), = windows(world, PARAMS, Cube(), solved.route, 600, solved.end_tick + 2)
    # The solver's ticks start one earlier than a replay's (it starts already moving).
    game_lo, game_hi = 119 - 1, 173 - 1
    assert lo >= game_lo - 2 and hi <= game_hi + 2
    if press + WINDOW_REACH > game_hi:
        assert abs(hi - game_hi) <= 1
    if press - WINDOW_REACH < game_lo:
        assert abs(lo - game_lo) <= 1


def test_a_wall_is_impossible() -> None:
    wall = [{"id": 1, "x": 255, "y": 15 + 30 * row} for row in range(5)]
    solved = solve(_world(wall), PARAMS, Cube(), 600)
    assert solved.result == "impossible"
    assert solved.furthest_x < 255


def test_route_description_reports_windows_in_ms() -> None:
    world = _world([{"id": 8, "x": 255, "y": 15}])
    solved = solve(world, PARAMS, Cube(), 600)
    described = describe_route(world, PARAMS, Cube(), solved, 600)
    press = described["route"][0]
    assert press["window_ms"] > 100
    assert described["difficulty"]["frame_perfect"] == 0


def test_an_orb_is_used_when_the_ground_alone_cannot_make_it() -> None:
    """A spike pit too long for one jump, with a yellow orb above its middle."""
    pit = [{"id": 8, "x": 15 + 30 * c, "y": 15} for c in range(10, 17)]
    orb = [{"id": 36, "x": 15 + 30 * 13, "y": 75}]
    without = solve(_world(pit), PARAMS, Cube(), 700)
    with_orb = solve(_world(pit + orb), PARAMS, Cube(), 700)
    assert without.result == "impossible"
    assert with_orb.result == "possible" and len(with_orb.route) == 2


# -- verify_section --------------------------------------------------------------------


def _verify(objects: list[dict], args: dict) -> dict:
    from gd_ai_assistant.design.sim.verify import verify

    return verify(_world(objects), PARAMS, args)


def test_verify_possible_reports_route_windows_and_exit() -> None:
    result = _verify([{"id": 8, "x": 255, "y": 15}], {"x_range": [0, 600]})
    assert result["result"] == "possible"
    assert result["constants"] == "calibrated"
    assert len(result["route"]) == 1 and result["route"][0]["window_ms"] > 0
    assert result["deaths_without_input"][0]["x"] < 255
    assert result["exit"]["grounded"] is True


def test_verify_impossible_says_how_far_and_what_killed() -> None:
    wall = [{"id": 1, "x": 255, "y": 15 + 30 * row} for row in range(5)]
    result = _verify(wall, {"x_range": [0, 600]})
    assert result["result"] == "impossible"
    assert result["furthest_x"] < 255
    assert result["frontier_causes"]


def test_verify_refuses_unsupported_content() -> None:
    ship = [{"id": 13, "x": 255, "y": 45}]
    result = _verify(ship, {"x_range": [0, 600]})
    assert result["result"] == "unverifiable"
    assert "not simulated" in result["unverifiable"][0]["reason"]


def test_verify_explicit_start_mid_level() -> None:
    spike = [{"id": 8, "x": 3015, "y": 15}]
    result = _verify(spike, {"x_range": [2700, 3300], "start_from": "explicit",
                             "start": {"mode": "cube", "speed": "1x", "row": 0}})
    assert result["result"] == "possible" and len(result["route"]) == 1
    ship = _verify(spike, {"x_range": [2700, 3300], "start_from": "explicit",
                           "start": {"mode": "ship"}})
    assert ship["result"] == "unverifiable"
