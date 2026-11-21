"""Route search and timing windows (LEVEL-DESIGN-SPEC.md 6.8)."""

from __future__ import annotations

from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import Cube, CubeParams, simulate
from gd_ai_assistant.design.sim.solver import (
    SolveResult,
    centre_inputs,
    commands,
    describe_route,
    input_events,
    release_windows,
    solve,
    windows,
)
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
    robot = [{"id": 745, "x": 255, "y": 45}]           # Sim-3
    result = _verify(robot, {"x_range": [0, 600]})
    assert result["result"] == "unverifiable"
    assert "Sim-3" in result["unverifiable"][0]["reason"]


def test_a_ship_holds_over_a_spike() -> None:
    ship = [{"id": 13, "x": 255, "y": 45}]
    flown = _verify(ship, {"x_range": [0, 900]})
    assert flown["result"] == "possible" and flown["exit"]["mode"] == "ship"
    spiked = _verify([*ship, {"id": 8, "x": 615, "y": 15}], {"x_range": [0, 900]})
    assert spiked["result"] == "possible", spiked
    (hold,) = spiked["route"]
    assert hold["action"] == "hold" and hold["release_tick"] > hold["tick"]
    assert hold["hold_ms"] > 0 and hold["release_window_ms"] > 0
    # A column of orbs it must fly through (P18: orbs fire only on a press).
    column = [{"id": 36, "x": 615, "y": 30 * r + 15} for r in range(10)]
    assert _verify([*ship, *column], {"x_range": [0, 900]})["result"] == "possible"


def _flight(mode_id: int, obstacles: list[dict]) -> tuple[World, SolveResult]:
    world = _world([{"id": mode_id, "x": 255, "y": 45}, *obstacles])
    return world, solve(world, PARAMS, Cube(), 1000)


def _spike(col: int, row: int = 0) -> dict:
    return {"id": 8, "x": 30 * col + 15, "y": 30 * row + 15}


def _block(col: int, row: int) -> dict:
    return {"id": 1, "x": 30 * col + 15, "y": 30 * row + 15}


def test_hold_routes_replay_to_the_end() -> None:
    """Ship through a tunnel, wave over a row of spikes: each route, played as inputs,
    reaches the end, and centring it keeps it finishing."""
    tunnel = [*[_spike(c) for c in range(18, 28)], *[_block(c, 4) for c in range(18, 28)]]
    for mode_id, obstacles in ((13, tunnel), (660, [_spike(c) for c in range(18, 24)])):
        world, solved = _flight(mode_id, obstacles)
        assert solved.result == "possible", (mode_id, solved.reason)
        assert len(solved.releases) == len(solved.route)
        assert any(r > t + 1 for t, r in zip(solved.route, solved.releases, strict=True))
        assert solved.end_tick is not None
        run = simulate(world, PARAMS, input_events(solved.route, solved.releases),
                       max_ticks=solved.end_tick + 2, until_x=1000, start=Cube(),
                       start_tick=0)
        assert run.outcome == "reached"
        route, releases = centre_inputs(world, PARAMS, Cube(), solved.route,
                                        solved.releases, 1000, solved.end_tick + 2)
        run = simulate(world, PARAMS, input_events(route, releases),
                       max_ticks=solved.end_tick + 4 * 24, until_x=1000, start=Cube(),
                       start_tick=0)
        assert run.outcome == "reached"


def test_a_wave_corridor_is_measured_on_a_route_that_keeps_its_distance() -> None:
    """eval wave-easy (2026-11-17), rebuilt: in a 90-unit corridor with small spikes the
    first route found bounced from floor to ceiling, two ticks from death at every input
    (8.3 ms); a player down the middle has far more. The forgiving route keeps a margin,
    finishes without it, and its windows are measured in the game's physics."""
    from dataclasses import replace

    from gd_ai_assistant.design.sim.solver import describe_forgiving, forgiving

    floor = [b for c in range(14, 51) for b in (_block(c, 0), _block(c, 1))]
    ceiling = [_block(c, 5) for c in range(12, 51)]
    spikes = [{"id": 39, "x": 30 * c + 15, "y": 66} for c in (18, 31, 44)]
    spikes += [{"id": 39, "x": 30 * c + 15, "y": 135, "properties": {"rotation": 180}}
               for c in (24, 38)]
    world = _world([{"id": 660, "x": 315, "y": 45}, *floor, *ceiling, *spikes])
    end = 1650
    solved = solve(world, PARAMS, Cube(), end)
    assert solved.result == "possible"
    raw = describe_route(world, PARAMS, Cube(), solved, end)["difficulty"]
    kept, described = describe_forgiving(world, PARAMS, Cube(), solved, end)
    assert kept is not solved and kept.clearance > 0
    # It survives with its clearance, so without.
    assert simulate(world, replace(PARAMS, clearance=kept.clearance),
                    input_events(kept.route, kept.releases), max_ticks=kept.end_tick + 2,
                    until_x=end, start=Cube(), start_tick=0).outcome == "reached"
    eased = described["difficulty"]
    assert raw["min_window_ms"] < 20 and eased["min_window_ms"] >= 50, (raw, eased)
    assert eased["clearance"] == kept.clearance
    # A route of taps is left alone.
    spike = _world([{"id": 8, "x": 255, "y": 15}])
    tapped = solve(spike, PARAMS, Cube(), 600)
    assert forgiving(spike, PARAMS, Cube(), 600, tapped) is tapped


def test_a_route_with_room_is_kept_without_a_second_search() -> None:
    """A wave over a row of spikes, its windows already wide: `describe_forgiving` keeps
    the route found (a margin's route can measure narrower: the estimate is rough)."""
    from gd_ai_assistant.design.sim.solver import describe_forgiving

    world, solved = _flight(660, [_spike(c) for c in range(18, 24)])
    raw = describe_route(world, PARAMS, Cube(), solved, 1000)
    kept, described = describe_forgiving(world, PARAMS, Cube(), solved, 1000)
    if raw["difficulty"]["min_window_ms"] >= 50:
        assert kept is solved and described == raw
    else:
        assert described["difficulty"]["min_window_ms"] >= raw["difficulty"]["min_window_ms"]


def test_a_ufo_presses_in_the_air() -> None:
    world, solved = _flight(111, [_spike(20), _spike(20, 1), _spike(20, 2)])
    assert solved.result == "possible" and len(solved.route) >= 2
    assert solved.releases == [t + 1 for t in solved.route]
    wall = [_block(20, r) for r in range(10)]
    assert _flight(111, wall)[1].result == "impossible"


def test_a_hold_has_a_press_and_a_release_window() -> None:
    world, solved = _flight(13, [_spike(20)])
    assert solved.result == "possible" and solved.end_tick is not None
    limit = solved.end_tick + 2
    (press,) = windows(world, PARAMS, Cube(), solved.route, 1000, limit, solved.releases)
    (release,) = release_windows(world, PARAMS, Cube(), solved.route, solved.releases,
                                 1000, limit)
    assert press[0] <= solved.route[0] <= press[1]
    assert release is not None and release[0] <= solved.releases[0] <= release[1]
    assert release_windows(world, PARAMS, Cube(), [5], [6], 1000, limit) == [None]


def test_commands_release_before_pressing_on_the_same_tick() -> None:
    assert commands([3, 10], [10, 11]) == {3: ["press"], 10: ["release", "press"],
                                           11: ["release"]}
    assert input_events([3], None, offset=2) == [{"tick": 5, "action": "press"},
                                                  {"tick": 6, "action": "release"}]


def test_a_ball_flips_over_a_spike() -> None:
    ball = [{"id": 47, "x": 255, "y": 45}]
    result = _verify([*ball, {"id": 8, "x": 615, "y": 15}], {"x_range": [0, 900]})
    assert result["result"] == "possible" and result["exit"]["mode"] == "ball"


def test_verify_explicit_start_mid_level() -> None:
    spike = [{"id": 8, "x": 3015, "y": 15}]
    result = _verify(spike, {"x_range": [2700, 3300], "start_from": "explicit",
                             "start": {"mode": "cube", "speed": "1x", "row": 0}})
    assert result["result"] == "possible" and len(result["route"]) == 1
    ship = _verify(spike, {"x_range": [2700, 3300], "start_from": "explicit",
                           "start": {"mode": "ship"}})
    assert ship["result"] == "unverifiable"


def test_start_positions_do_not_block_verification() -> None:
    result = _verify([{"id": 31, "x": 75, "y": 15}, {"id": 8, "x": 255, "y": 15}],
                     {"x_range": [0, 600]})
    assert result["result"] == "possible"


def test_tricky_columns_beaten_by_hand() -> None:
    """A section a strong player built and beat by hand (2026-11-08): possible, but only
    just. At 0.5x, each column needs its press on one exact tick (4.2 ms); one tick
    early lands on the small spike after the block, one tick late touches the one before
    it. (With the 1x jump, before the 0.5x jump was measured on 2026-11-09, the windows
    were 4 ticks; the game showed the 1x jump is wrong at 0.5x.)

    Small hitboxes, slow speed and a tight arc: if a change to gravity, jump strength or
    hitboxes turns this impossible, the change is wrong, whatever else it fixes.
    """
    import json
    from pathlib import Path

    from gd_ai_assistant.design.sim.cube import simulate

    fixture = Path(__file__).parent / "fixtures" / "tricky_columns.json"
    world = _world(json.loads(fixture.read_text(encoding="utf-8"))["objects"])
    solved = solve(world, PARAMS, Cube(), 420)
    assert solved.result == "possible" and len(solved.route) == 2
    spans = windows(world, PARAMS, Cube(), solved.route, 420, solved.end_tick + 2)
    assert [hi - lo + 1 for lo, hi in spans] == [1, 1]

    def first_column(press: int) -> bool:
        taps = [{"tick": press, "action": "press"}, {"tick": press + 1, "action": "release"}]
        return simulate(world, PARAMS, taps, max_ticks=400, until_x=240, start=Cube(),
                        start_tick=0).death is None

    lo, hi = spans[0]
    assert all(first_column(t) for t in range(lo, hi + 1))
    assert not first_column(lo - 1) and not first_column(hi + 1)
