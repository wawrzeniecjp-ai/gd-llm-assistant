"""`verify_section` (LEVEL-DESIGN-SPEC.md 6.9): can a stretch of the level be played?"""

from __future__ import annotations

import time
from typing import Any

from ..bands import judge
from ..constants import SPEEDS, PhysicsConstants, load_constants
from ..level_model import LevelModel
from .cube import HALF, Cube, CubeParams, UncalibratedError, simulate
from .solver import describe_forgiving, input_events, solve
from .world import World, world_from_model

#: Most unsupported objects listed in a result; the rest are counted.
_MAX_LISTED = 20


def _start_state(args: dict[str, Any], x0: float) -> tuple[Cube | None, float, str | None]:
    """(cube, start x, reason it cannot be simulated)."""
    start_from = args.get("start_from", "level_start")
    if start_from in ("level_start", "auto"):
        # The level's own start speed, which the agent reads from the level settings
        # (LEVEL-DESIGN-SPEC.md 8.1); 1x when the mod does not report it.
        speed = args.get("_level_start_speed") or "1x"
        if speed not in SPEEDS:
            speed = "1x"
        return Cube(speed=speed), 0.0, None
    if start_from != "explicit":
        return None, 0.0, f"unknown start_from {start_from!r}"
    start = args.get("start") or {}
    mode = start.get("mode", "cube")
    if mode != "cube":
        return None, x0, f"the simulator handles the cube only so far, not {mode}"
    if start.get("gravity", 1) != 1 or start.get("mini", False):
        return None, x0, "flipped gravity and mini size are not simulated yet"
    speed = start.get("speed", "1x")
    if speed not in SPEEDS:
        return None, x0, f"unknown speed {speed!r}"
    row = int(start.get("row", 0))
    cube = Cube(x=x0, y=HALF + 30.0 * row, vy=float(start.get("vy", 0.0)), speed=speed,
                grounded=row == 0 and float(start.get("vy", 0.0)) == 0.0)
    return cube, x0, None


def verify(world: World, params: CubeParams, args: dict[str, Any]) -> dict[str, Any]:
    began = time.perf_counter()
    x_range = args.get("x_range")
    if not x_range or len(x_range) != 2:
        return {"error": "give `x_range` as [start, end] in level units"}
    x0, x1 = sorted(float(v) for v in x_range)
    cube, sx, reason = _start_state(args, x0)
    base: dict[str, Any] = {
        "constants": "calibrated" if params.calibrated else "uncalibrated",
        "start": {"x": sx, "from": args.get("start_from", "level_start")},
    }
    if cube is None:
        return {**base, "result": "unverifiable", "unverifiable": [{"reason": reason}]}

    blocked = world.unsupported_between(sx - 60, x1)
    if blocked:
        return {
            **base, "result": "unverifiable",
            "unverifiable": blocked[:_MAX_LISTED],
            "unverifiable_count": len(blocked),
            "note": "The simulator plays the cube with solids, hazards, yellow, pink and "
                    "blue orbs and pads, and speed, gravity, size and mode portals; ship, "
                    "ball, ufo and wave, mini or not, with blocks, spikes, orbs and pads. "
                    "A mini cube's orbs, a mini cube squeezed under a low block, a flying "
                    "player meeting a block from below deeper than measured, a mini "
                    "robot's orbs, the spider and the swing are not calibrated yet and "
                    "stop it.",
        }

    idle = simulate(world, params, [], max_ticks=10**6, until_x=x1, start=cube,
                    start_tick=0, record=False)
    deaths_without_input = []
    if idle.death is not None and idle.death.cause != "unverifiable":
        deaths_without_input.append({"x": round(idle.death.x, 1),
                                     "cause_uid": idle.death.cause_uid})

    solved = solve(world, params, cube, x1, budget_s=float(args.get("budget_s", 10.0)))
    result: dict[str, Any] = {**base, "result": solved.result,
                              "deaths_without_input": deaths_without_input}
    if solved.result == "possible":
        # A tight ship or wave route is compared with one that keeps its distance: the
        # windows a player flying it would have.
        # From the level start, only the presses inside the asked-for range matter to
        # the caller, and only they are measured; the approach is counted.
        from_x = None if args.get("start_from", "level_start") == "explicit" else x0
        solved, described = describe_forgiving(world, params, cube, solved, x1, from_x)
        result.update(described)
        judged = judge(described, args.get("difficulty"))
        if judged is not None:
            result.update(judged)
        final = solved.final
        assert final is not None
        result["exit"] = {"x": round(final.x, 1), "y": round(final.y, 2),
                          "vy": round(final.vy, 3), "mode": final.mode, "speed": final.speed,
                          "gravity": final.gravity, "mini": final.mini,
                          "grounded": final.grounded}
    elif solved.result == "unverifiable":
        first = solved.frontier_deaths[0] if solved.frontier_deaths else None
        result["unverifiable"] = [{"reason": solved.reason or "not simulated",
                                   **({"x": round(first.x, 1)} if first else {})}]
        result["furthest_x"] = round(solved.furthest_x, 1)
    else:
        result["furthest_x"] = round(solved.furthest_x, 1)
        causes = sorted({d.cause_uid for d in solved.frontier_deaths if d.cause_uid})
        result["frontier_causes"] = causes[:_MAX_LISTED]
        if solved.reason:
            result["reason"] = solved.reason
    result["elapsed_ms"] = round((time.perf_counter() - began) * 1000)
    return result


def verify_section(model: LevelModel, args: dict[str, Any],
                   constants: PhysicsConstants | None = None) -> dict[str, Any]:
    """The tool: the level snapshot, the calibrated cube, and `verify`."""
    try:
        params = CubeParams.from_constants(constants or load_constants())
    except UncalibratedError as exc:
        return {"result": "unverifiable",
                "unverifiable": [{"reason": f"constants not calibrated for cube: {exc}"}]}
    return verify(world_from_model(model), params, args)


#: Columns of existing level verified either side of a placed block (LEVEL-DESIGN-SPEC.md 5.4).
CONTEXT_COLS = 10


def verify_placement(model: LevelModel, creates: list[dict[str, Any]], deletes: list[int],
                     block_x: tuple[float, float], spec: dict[str, Any],
                     constants: PhysicsConstants | None = None) -> dict[str, Any]:
    """`place_grid`'s `verify`: the block as it would stand in the level, plus context.

    Works before anything is placed (preview) because the world is built from the
    snapshot minus the deletions plus the planned objects.
    """
    from dataclasses import replace

    from ..roles import load_role_table
    from .world import world_from_objects

    try:
        params = CubeParams.from_constants(constants or load_constants())
    except UncalibratedError as exc:
        return {"result": "unverifiable",
                "unverifiable": [{"reason": f"constants not calibrated for cube: {exc}"}]}
    removed = set(deletes)
    current = world_from_model(model)
    planned = world_from_objects(creates, load_role_table())
    world = World(
        [b for b in current.bodies if b.uid not in removed]
        + [replace(b, uid=-b.uid) for b in planned.bodies],
        [u for u in current.unsupported if u["uid"] not in removed] + planned.unsupported,
    )
    x0 = block_x[0] - CONTEXT_COLS * 30
    x1 = block_x[1] + CONTEXT_COLS * 30
    args = {
        "x_range": [x0, x1],
        "start_from": "explicit",
        "start": {"mode": spec.get("gamemode", "cube"), "speed": spec.get("speed", "1x"),
                  "row": int(spec.get("entry_row", 0))},
    }
    result = verify(world, params, args)
    result["note"] = ("Planned objects have negative uids in this result. The start is "
                      f"assumed: {args['start']} at x={x0:g}.")
    return result


def route_overlay(model: LevelModel, x_end: float,
                  constants: PhysicsConstants | None = None,
                  start_speed: str = "1x") -> dict[str, Any]:
    """The solver's route from the level start to `x_end`, as points to draw: the path,
    each press, and the x span its timing window covers (LEVEL-DESIGN-SPEC.md 13.2)."""
    try:
        params = CubeParams.from_constants(constants or load_constants())
    except UncalibratedError as exc:
        return {"result": "unverifiable", "reason": str(exc)}
    world = world_from_model(model)
    blocked = world.unsupported_between(-60, x_end)
    if blocked:
        return {"result": "unverifiable", "reason": blocked[0]["reason"]}
    start = Cube(speed=start_speed if start_speed in SPEEDS else "1x")
    solved = solve(world, params, start, x_end)
    if solved.result != "possible":
        return {"result": solved.result, "furthest_x": solved.furthest_x}
    solved, described = describe_forgiving(world, params, start, solved, x_end)
    run = simulate(world, params, input_events(solved.route, solved.releases or None),
                   max_ticks=(solved.end_tick or 0) + 2,
                   until_x=x_end, start=start, start_tick=0)
    xs = dict(zip(run.trace["tick"], run.trace["x"], strict=True))
    ys = dict(zip(run.trace["tick"], run.trace["y"], strict=True))
    presses = []
    for item in described["route"]:
        lo, hi = item["window_ticks"]
        presses.append({"x": xs.get(item["tick"] + 1, item["x"]),
                        "y": ys.get(item["tick"] + 1, 15.0),
                        "x_lo": xs.get(lo + 1, item["x"]), "x_hi": xs.get(hi + 1, item["x"]),
                        "ms": item["window_ms"]})
    return {"result": "possible",
            "points": list(zip(run.trace["x"], run.trace["y"], strict=True)),
            "presses": presses}
