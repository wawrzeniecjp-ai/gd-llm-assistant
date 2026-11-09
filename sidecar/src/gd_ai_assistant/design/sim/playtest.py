"""`playtest_verify` (LEVEL-DESIGN-SPEC.md 7.3): the solver's route, played in the game.

The solver finds a route through a section; the mod's `run_replay` plays exactly those
presses in the editor's playtest; the result says whether the game agrees. With `edges`,
every press is also replayed at both ends of its timing window and one tick past each,
which checks the window itself.

Each press is played in the middle of its timing window (`centre_route`), not on the
last tick that works, where the search leaves it: there a simulator that is one tick or
one unit off turns a sound route into a death in the game.

With `traces`, a run where the game and the simulator part ways (or the game dies) keeps
both traces tick by tick under `trace_rows`, as TRACE_COLUMNS, for diagnosis. The
assistant does not ask for them; `scripts/check_playtest.py` does.

Tick numbering. The solver starts already moving: its tick 1 is the first move. A replay
from the level start spends its tick 1 standing still, so game tick = solver tick + 1
(`LEAD`, measured by calibration P5). From a start position this is not measured yet, so
the run measures it: the first tick on which the game's x changes gives the lead, tick 0
gives the start position, and a run that disagrees with the assumption is solved and
replayed again from what the game showed.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..constants import PhysicsConstants, load_constants
from ..level_model import LevelModel
from ..roles import Role
from .cube import HALF, Cube, CubeParams, UncalibratedError, simulate
from .solver import WINDOW_REACH, centre_route, solve, windows
from .world import World, world_from_model

#: `request(type, payload, timeout=seconds)`: the server's `request`, or a fake.
Request = Callable[..., Awaitable[Any]]

#: Replay ticks before the first move, from the level start (calibration P5).
LEAD = 1
#: Ticks replayed past the route's end, so the run clearly finishes alive.
TAIL_TICKS = 24
#: The mod's caps (src/exec/replay.cpp).
MAX_REPLAY_TICKS = 14400
MAX_SAMPLES = 6000
#: Agreement with the simulator, LEVEL-DESIGN-SPEC.md 6.10.
MAX_DX = 0.5
MAX_DY = 1.0
#: A start this far from where it was assumed is solved again from what the game showed.
START_TOLERANCE = 1.0
#: Most replays one call may run (each is a real playtest, about the section's length).
MAX_REPLAYS = 40
#: One row of `trace_rows`: the game's state, then the simulator's (None past its end).
TRACE_COLUMNS = ["game_tick", "x", "y", "vy", "speed", "grounded",
                 "sim_x", "sim_y", "sim_vy", "sim_grounded"]


class PlaytestError(Exception):
    """The game could not run the replay (editor busy, cancelled by the user, ...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _taps(presses: list[int], lead: int) -> list[dict[str, Any]]:
    out = []
    for t in sorted(presses):
        out.append({"tick": t + lead, "action": "press"})
        out.append({"tick": t + lead + 1, "action": "release"})
    return out


def pick_start(model: LevelModel, x0: float, spec: Any) -> dict[str, Any]:
    """Where the replay starts: a start position's uid, or the level start.

    `auto` takes the furthest-right start position at or before the section's start
    (within a column), and the level start when there is none.
    """
    positions = sorted((o for o in model if o.role is Role.START_POS), key=lambda o: o.x)
    if spec in (None, "auto"):
        before = [o for o in positions if o.x <= x0 + 30]
        if not before:
            return {"kind": "level_start"}
        chosen = before[-1]
        return {"kind": "start_pos", "uid": chosen.uid, "x": chosen.x, "y": chosen.y}
    if spec == "level_start":
        return {"kind": "level_start"}
    if isinstance(spec, dict) and "start_pos" in spec:
        uid = int(spec["start_pos"])
        for o in positions:
            if o.uid == uid:
                return {"kind": "start_pos", "uid": uid, "x": o.x, "y": o.y}
        raise ValueError(f"no start position with uid {uid}")
    raise ValueError("`start` must be 'auto', 'level_start' or {\"start_pos\": uid}")


def _start_cube(start: dict[str, Any]) -> Cube:
    if start["kind"] == "level_start":
        return Cube()
    y = float(start["y"])
    return Cube(x=float(start["x"]), y=y, grounded=abs(y - HALF) < 0.5)


async def _replay(request: Request, start: dict[str, Any], inputs: list[dict[str, Any]],
                  ticks: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "start": ({"kind": "start_pos", "uid": start["uid"]}
                  if start["kind"] == "start_pos" else {"kind": "level_start"}),
        "inputs": inputs,
        "max_ticks": min(ticks, MAX_REPLAY_TICKS),
        "sample_every": max(1, math.ceil(ticks / MAX_SAMPLES)),
    }
    # The mod gives up on a stalled run after 4x its length plus 10 s; wait a bit longer.
    timeout = payload["max_ticks"] / 240.0 * 4.0 + 30.0
    response = await request("run_replay", payload, timeout=timeout)
    if not response.ok:
        error = response.error or {}
        raise PlaytestError(str(error.get("code", "ERROR")),
                            str(error.get("message", "run_replay failed")))
    return response.result or {}


def _lead(trace: dict[str, list[Any]]) -> int | None:
    """Replay ticks before the first move: the tick before x first changes."""
    xs, ticks = trace.get("x") or [], trace.get("tick") or []
    for i in range(1, len(xs)):
        if abs(xs[i] - xs[0]) > 1e-3:
            return int(ticks[i]) - 1
    return None


def _reached(game: dict[str, Any], x1: float) -> bool:
    trace = game.get("trace") or {}
    xs = trace.get("x") or []
    if game.get("outcome") == "death":
        return False
    return bool(xs) and max(xs) >= x1 - 1.0


def _compare(world: World, p: CubeParams, start: Cube, presses: list[int], lead: int,
             game: dict[str, Any], keep: bool = False) -> dict[str, Any]:
    """The game's trace against the simulator's, tick by tick.

    With `keep`, a comparison that disagrees, or a run that died, carries both traces as
    `trace_rows` (TRACE_COLUMNS).
    """
    trace = game.get("trace") or {}
    ticks = trace.get("tick") or []
    if not ticks:
        return {"compared_ticks": 0}
    sim = simulate(world, p, [{"tick": t, "action": a} for r in presses
                              for t, a in ((r, "press"), (r + 1, "release"))],
                   max_ticks=max(ticks) - lead + 2, start=start, start_tick=0)
    simulated = {t: (x, y, vy, g) for t, x, y, vy, g in zip(
        sim.trace["tick"], sim.trace["x"], sim.trace["y"], sim.trace["vy"],
        sim.trace["grounded"], strict=True)}
    worst_dx = worst_dy = 0.0
    first: dict[str, Any] | None = None
    compared = 0
    last = ticks[-1]
    for tick, x, y in zip(ticks, trace["x"], trace["y"], strict=True):
        if game.get("outcome") == "death" and tick == last:
            continue  # the death sample is labelled one tick apart (test_sim_golden)
        s = simulated.get(int(tick) - lead)
        if s is None:
            continue
        compared += 1
        dx, dy = abs(s[0] - x), abs(s[1] - y)
        worst_dx, worst_dy = max(worst_dx, dx), max(worst_dy, dy)
        if first is None and (dx > MAX_DX or dy > MAX_DY):
            first = {"game_tick": int(tick), "game": [round(x, 2), round(y, 2)],
                     "simulated": [round(s[0], 2), round(s[1], 2)]}
    out: dict[str, Any] = {"compared_ticks": compared, "max_dx": round(worst_dx, 3),
                           "max_dy": round(worst_dy, 3), "agrees": first is None}
    if first is not None:
        out["first_divergence"] = first
    if sim.death is not None:
        out["simulated_death_x"] = round(sim.death.x, 1)
    if keep and (first is not None or game.get("outcome") == "death"):
        out["trace_rows"] = _rows(trace, simulated, lead)
    return out


def _rows(trace: dict[str, list[Any]], simulated: dict[int, tuple[float, ...]],
          lead: int) -> list[list[Any]]:
    """Both traces side by side, one TRACE_COLUMNS row per game sample."""
    def col(name: str, i: int) -> Any:
        values = trace.get(name) or []
        return values[i] if i < len(values) else None

    def r(v: Any) -> Any:
        return round(float(v), 3) if isinstance(v, int | float) else v

    rows = []
    for i, tick in enumerate(trace.get("tick") or []):
        s = simulated.get(int(tick) - lead)
        rows.append([int(tick), r(col("x", i)), r(col("y", i)), r(col("vy", i)),
                     r(col("speed", i)), col("grounded", i),
                     *((r(s[0]), r(s[1]), r(s[2]), int(s[3])) if s else (None,) * 4)])
    return rows


def _end_tick(world: World, p: CubeParams, start: Cube, route: list[int], x1: float,
              fallback: int) -> int:
    """The tick the route reaches `x1` (moving presses can move it, past a pad say)."""
    sim = simulate(world, p, [{"tick": t, "action": a} for r in route
                              for t, a in ((r, "press"), (r + 1, "release"))],
                   max_ticks=fallback + 4 * WINDOW_REACH, until_x=x1, start=start,
                   start_tick=0, record=False)
    return sim.ticks if sim.outcome == "reached" else fallback


async def playtest_verify(request: Request, model: LevelModel, args: dict[str, Any],
                          constants: PhysicsConstants | None = None) -> dict[str, Any]:
    """Solve the section, play the route in the game, and compare (see module doc)."""
    began = time.perf_counter()
    x_range = args.get("x_range")
    if not x_range or len(x_range) != 2:
        return {"error": "give `x_range` as [start, end] in level units"}
    x0, x1 = sorted(float(v) for v in x_range)
    try:
        params = CubeParams.from_constants(constants or load_constants())
    except UncalibratedError as exc:
        return {"result": "unverifiable", "reason": f"constants not calibrated: {exc}"}
    try:
        start = pick_start(model, x0, args.get("start", "auto"))
    except ValueError as exc:
        return {"error": str(exc)}
    world = world_from_model(model)
    cube = _start_cube(start)
    blocked = world.unsupported_between(cube.x - 60, x1)
    if blocked:
        return {"result": "unverifiable", "start": start, "unverifiable": blocked[:20],
                "reason": "the span holds something the simulator does not handle, so "
                          "there is no route to play"}

    lead = LEAD
    replays = 0
    corrections: list[str] = []
    budget = int(args.get("max_replays", MAX_REPLAYS))

    async def play(presses: list[int], end_tick: int) -> dict[str, Any]:
        nonlocal replays
        if replays >= budget:
            raise PlaytestError("REPLAY_BUDGET", f"more than {budget} replays needed")
        replays += 1
        return await _replay(request, start, _taps(presses, lead),
                             end_tick + lead + TAIL_TICKS)

    try:
        for _attempt in range(2):
            solved = solve(world, params, cube, x1)
            if solved.result != "possible":
                return {"result": f"not_played_{solved.result}", "start": start,
                        "solver": solved.result, "furthest_x": round(solved.furthest_x, 1),
                        "note": "the solver found no route, so there is nothing to play; "
                                "fix the section with verify_section first"}
            assert solved.end_tick is not None
            route = centre_route(world, params, cube, solved.route, x1, solved.end_tick + 2)
            end_tick = _end_tick(world, params, cube, route, x1, solved.end_tick)
            game = await play(route, end_tick)
            trace = game.get("trace") or {}
            # Check the assumptions about the start against what the game did.
            observed_lead = _lead(trace)
            xs, ys = trace.get("x") or [], trace.get("y") or []
            observed_x = float(xs[0]) if xs else cube.x
            observed_y = float(ys[0]) if ys else cube.y
            moved = abs(observed_x - cube.x) > START_TOLERANCE or abs(
                observed_y - cube.y) > START_TOLERANCE
            relead = observed_lead is not None and observed_lead != lead
            if not (moved or relead):
                break
            if moved:
                corrections.append(f"the game started at ({observed_x:.1f}, {observed_y:.1f}),"
                                   f" not ({cube.x:.1f}, {cube.y:.1f}); solved again from "
                                   "there")
                cube = Cube(x=observed_x, y=observed_y, grounded=abs(observed_y - HALF) < 0.5)
            if relead:
                corrections.append(f"the game's first move was after {observed_lead} "
                                   f"ticks, not {lead}; replayed with that offset")
                lead = int(observed_lead)
        else:
            return {"result": "inconclusive", "start": start, "corrections": corrections,
                    "reason": "the game's start did not settle after a correction"}

        reached = _reached(game, x1)
        keep = bool(args.get("traces"))
        comparison = _compare(world, params, cube, route, lead, game, keep)
        spans = windows(world, params, cube, route, x1, end_tick + 2)
        result: dict[str, Any] = {
            "result": "confirmed" if reached else "failed_in_game",
            "start": start,
            "route": [{"tick": t, "game_tick": t + lead, "window_ticks": [lo, hi]}
                      for t, (lo, hi) in zip(route, spans, strict=True)],
            "game": {"outcome": game.get("outcome"), "ticks": game.get("ticks"),
                     "death": game.get("death")},
            "comparison": comparison,
        }
        if corrections:
            result["corrections"] = corrections
        if not reached:
            result["note"] = ("the simulator's route dies in the game: the simulator is "
                              "wrong here. Do not tell the user this section is verified.")

        if args.get("edges") and reached and route:

            def compare(presses: list[int], played: dict[str, Any]) -> dict[str, Any]:
                return _compare(world, params, cube, presses, lead, played, keep)

            result["edges"] = []
            for index, (tick, (lo, hi)) in enumerate(zip(route, spans, strict=True)):
                result["edges"].append(await _edges(play, compare, route, index, tick, lo,
                                                    hi, end_tick, x1))
            result["edges_agree"] = all(e["agrees"] for e in result["edges"])
    except PlaytestError as exc:
        return {"result": "error", "code": exc.code, "error": str(exc), "start": start,
                "replays": replays}
    result["replays"] = replays
    result["elapsed_s"] = round(time.perf_counter() - began, 1)
    return result


async def _edges(play: Callable[[list[int], int], Awaitable[dict[str, Any]]],
                 compare: Callable[[list[int], dict[str, Any]], dict[str, Any]],
                 route: list[int], index: int, tick: int, lo: int, hi: int,
                 end_tick: int, x1: float) -> dict[str, Any]:
    """Replay one press at its window's ends and one tick past each.

    An end the window search stopped at because of its reach (WINDOW_REACH ticks), not
    because the route failed, is not an edge, so the tick past it is not expected to die.
    """
    checks: list[tuple[str, int, bool | None]] = [("lo", lo, True), ("hi", hi, True)]
    checks.append(("lo-1", lo - 1, None if lo <= tick - WINDOW_REACH else False))
    checks.append(("hi+1", hi + 1, None if hi >= tick + WINDOW_REACH else False))
    out: dict[str, Any] = {"press": index, "tick": tick, "window_ticks": [lo, hi],
                           "checks": [], "agrees": True}
    others = set(route[:index] + route[index + 1:])
    for label, moved, expect in checks:
        if expect is None or moved < 0 or moved in others:
            out["checks"].append({"at": label, "tick": moved, "skipped": True})
            continue
        presses = sorted([*route[:index], moved, *route[index + 1:]])
        game = await play(presses, end_tick + 4)
        survived = _reached(game, x1)
        check = {"at": label, "tick": moved, "expected": "survive" if expect else "die",
                 "game": "survived" if survived else "died", "agrees": survived == expect}
        if not survived and game.get("death"):
            check["death_x"] = round(float(game["death"].get("x", 0.0)), 1)
        comparison = compare(presses, game)
        check["max_dx"], check["max_dy"] = comparison.get("max_dx"), comparison.get("max_dy")
        if not check["agrees"] and "trace_rows" in comparison:
            check["trace_rows"] = comparison["trace_rows"]
        out["checks"].append(check)
        out["agrees"] = out["agrees"] and check["agrees"]
    return out
