"""`simulate_inputs` (LEVEL-DESIGN-SPEC.md 6.7): play a fixed input script in the simulator.

The script has the same shape as an in-game replay (7.2), so the two can be compared. An
input given by `x` happens on the first tick whose sample is at or past that x: the tick
is found while the run plays, because a jump snap or a speed portal changes where the cube
is on later ticks.
"""

from __future__ import annotations

from typing import Any

from ..constants import PhysicsConstants, load_constants
from ..level_model import LevelModel
from .cube import CubeParams, UncalibratedError, advance, commands
from .verify import _MAX_LISTED, _start_state
from .world import World, world_from_model

#: Trace rows returned at most; a longer run is sampled more sparsely (events are kept).
MAX_ROWS = 400
#: Default sampling stride, in ticks (LEVEL-DESIGN-SPEC.md 6.7).
SAMPLE_EVERY = 4
#: Inputs accepted in one script.
MAX_INPUTS = 500
_ACTIONS = ("press", "release")


def _parse_inputs(raw: Any) -> tuple[list[tuple[int, str]], list[tuple[float, str]], str | None]:
    """(by tick, by x, error)."""
    if raw is None:
        return [], [], None
    if not isinstance(raw, list):
        return [], [], "`inputs` must be a list of {tick or x, action}"
    if len(raw) > MAX_INPUTS:
        return [], [], f"at most {MAX_INPUTS} inputs"
    by_tick: list[tuple[int, str]] = []
    by_x: list[tuple[float, str]] = []
    for i, item in enumerate(raw):
        action = item.get("action") if isinstance(item, dict) else None
        if action not in _ACTIONS:
            return [], [], f"input {i}: action must be press or release"
        has_tick, has_x = item.get("tick") is not None, item.get("x") is not None
        if has_tick == has_x:
            return [], [], f"input {i}: give exactly one of tick and x"
        if has_tick:
            by_tick.append((int(item["tick"]), action))
        else:
            by_x.append((float(item["x"]), action))
    by_x.sort(key=lambda pair: pair[0])
    return by_tick, by_x, None


def run_inputs(world: World, params: CubeParams, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("until_x") is None:
        return {"error": "give `until_x`, the x where the run stops"}
    until_x = float(args["until_x"])
    start_from = args.get("start_from", "level_start")
    start_x = float((args.get("start") or {}).get("x", 0.0))
    cube, sx, reason = _start_state(args, start_x, params)
    base: dict[str, Any] = {
        "constants": "calibrated" if params.calibrated else "uncalibrated",
        "start": {"x": sx, "from": start_from},
    }
    if cube is None:
        return {**base, "outcome": "unverifiable", "unverifiable": [{"reason": reason}]}
    if until_x <= sx:
        return {"error": f"until_x {until_x:g} is not after the start x {sx:g}"}
    by_tick, by_x, error = _parse_inputs(args.get("inputs"))
    if error:
        return {"error": error}
    blocked = world.unsupported_between(sx - 60, until_x)
    if blocked:
        return {**base, "outcome": "unverifiable", "unverifiable": blocked[:_MAX_LISTED],
                "unverifiable_count": len(blocked)}

    scheduled: dict[int, list[str]] = {}
    for tick, action in by_tick:
        scheduled.setdefault(tick, []).append(action)
    pending = list(by_x)
    resolved: list[dict[str, Any]] = []
    rows: list[tuple[int, float, float, float, int]] = []
    events: list[dict[str, Any]] = []

    def record(t: int) -> None:
        rows.append((t, cube.x, cube.y, cube.vy, int(cube.grounded)))
        # Inputs by x happen on the first tick whose sample reaches their x.
        while pending and pending[0][0] <= cube.x:
            x, action = pending.pop(0)
            scheduled.setdefault(t, []).append(action)
            resolved.append({"x": x, "action": action, "tick": t})

    slowest = min(params.dx.values())
    max_ticks = int((until_x - sx) / slowest) + 480
    t = 0
    if start_from == "explicit":
        record(0)
    else:
        # The level start, as the game replays it: commands for tick 0 before any move.
        record(0)
        commands(cube, params, scheduled.get(0, ()))
        t = 1
        record(1)
    outcome, death = "max_ticks", None
    while t < max_ticks:
        t += 1
        before = (cube.grounded, cube.vy, cube.speed)
        actions = scheduled.get(t - 1, ())
        death = advance(cube, world, params, t, actions)
        record(t)
        if actions:
            events.append({"tick": t - 1, "event": "+".join(actions), "x": round(cube.x, 2)})
        if death is not None:
            outcome = "unverifiable" if death.cause == "unverifiable" else "death"
            break
        if before[0] and not cube.grounded:
            events.append({"tick": t, "event": "takeoff", "x": round(cube.x, 2),
                           "vy": round(cube.vy, 3)})
        elif not before[0] and cube.grounded:
            events.append({"tick": t, "event": "landing", "x": round(cube.x, 2),
                           "y": round(cube.y, 2)})
        elif not cube.grounded and abs(cube.vy - before[1]) > 1.0 and cube.mode == "cube":
            events.append({"tick": t, "event": "impulse", "x": round(cube.x, 2),
                           "vy": round(cube.vy, 3)})
        if cube.speed != before[2]:
            events.append({"tick": t, "event": f"speed {cube.speed}", "x": round(cube.x, 2)})
        if cube.x >= until_x:
            outcome = "reached"
            break

    stride = max(int(args.get("sample_every", SAMPLE_EVERY)), 1)
    while len(rows) // stride > MAX_ROWS:
        stride *= 2
    keep = {e["tick"] for e in events} | {e["tick"] + 1 for e in events}
    last = rows[-1][0]
    trace: dict[str, list[Any]] = {k: [] for k in ("tick", "x", "y", "vy", "grounded")}
    for row in rows:
        if row[0] % stride == 0 or row[0] in keep or row[0] == last:
            for key, value in zip(trace, row, strict=True):
                trace[key].append(round(value, 3) if isinstance(value, float) else value)

    result: dict[str, Any] = {**base, "outcome": outcome, "ticks": t,
                              "final": {"x": round(cube.x, 2), "y": round(cube.y, 2),
                                        "vy": round(cube.vy, 3), "speed": cube.speed,
                                        "grounded": cube.grounded, "mode": cube.mode,
                                        "gravity": cube.gravity, "mini": cube.mini},
                              "events": events, "trace": trace, "sample_every": stride}
    if death is not None and death.cause == "unverifiable":
        result["unverifiable"] = [{"reason": death.reason, "x": round(death.x, 2)}]
    elif death is not None:
        result["death"] = {"tick": death.tick, "x": round(death.x, 2), "y": round(death.y, 2),
                           "cause_uid": death.cause_uid, "cause": death.cause}
    if resolved:
        result["inputs_by_x"] = resolved
    unplayed = [{"x": x, "action": a} for x, a in pending]
    unplayed += [{"tick": tk, "action": a} for tk, a in by_tick if tk >= t]
    if unplayed:
        result["not_played"] = unplayed
    return result


def simulate_inputs(model: LevelModel, args: dict[str, Any],
                    constants: PhysicsConstants | None = None) -> dict[str, Any]:
    """The tool: the level snapshot, the calibrated cube, and `run_inputs`."""
    try:
        params = CubeParams.from_constants(constants or load_constants())
    except UncalibratedError as exc:
        return {"outcome": "unverifiable",
                "unverifiable": [{"reason": f"constants not calibrated for cube: {exc}"}]}
    return run_inputs(world_from_model(model), params, args)
