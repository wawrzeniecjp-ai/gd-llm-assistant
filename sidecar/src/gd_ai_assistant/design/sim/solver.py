"""Finding a route through a section, and how forgiving it is (LEVEL-DESIGN-SPEC.md 6.8).

Breadth-first by tick over decision points, with deduplication. The cube only has a
choice when a press would do something: it is on the ground (a jump) or touching an
unused orb (an orb jump). Every press is a tap: pressed on the decision tick, released
on the next. Between decision points the state advances deterministically.

Outcomes: `possible` (a route reaches the end alive), `impossible` (every branch died and
nothing was cut), `inconclusive` (the beam overflowed or the time budget ran out, so a
route may exist), `unverifiable` (the span holds something Sim-1 does not simulate).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .cube import Cube, CubeParams, Death, advance, simulate
from .world import World

DEFAULT_BEAM = 2000
DEFAULT_BUDGET_S = 10.0
WINDOW_REACH = 24            # ticks either side (100 ms), LEVEL-DESIGN-SPEC.md 6.8.3
TICK_MS = 1000.0 / 240.0


@dataclass
class Branch:
    cube: Cube
    presses: tuple[int, ...] = ()
    release_at: int | None = None     # tick whose commands release the current tap


@dataclass
class SolveResult:
    result: str
    route: list[int] = field(default_factory=list)       # press ticks
    furthest_x: float = 0.0
    frontier_deaths: list[Death] = field(default_factory=list)
    end_tick: int | None = None
    final: Cube | None = None
    reason: str | None = None
    elapsed_ms: float = 0.0


def _actions(branch: Branch, tick: int, press: bool) -> list[str]:
    actions = []
    if branch.release_at == tick:
        actions.append("release")
    if press:
        actions.append("press")
    return actions


def solve(world: World, p: CubeParams, start: Cube, end_x: float, *,
          beam: int = DEFAULT_BEAM, budget_s: float = DEFAULT_BUDGET_S,
          max_ticks: int | None = None) -> SolveResult:
    """Find a route from `start` to `end_x`.

    A coarse pass first (ground jumps only every `COARSE_STRIDE` ticks), which finds the
    usual route quickly; the window search then reports how exact it really is. Only
    when that fails does the full search run, and only the full search may answer
    `impossible`, so a jump that needs one exact tick is never missed.
    """
    began = time.perf_counter()
    if max_ticks is None:
        slowest = min(p.dx.values())
        max_ticks = int((end_x - start.x) / slowest) + 2 * 240
    coarse = _search(world, p, start, end_x, beam=beam, deadline=began + budget_s,
                     max_ticks=max_ticks, stride=COARSE_STRIDE)
    if coarse.result == "possible" or coarse.reason == "time budget":
        coarse.elapsed_ms = (time.perf_counter() - began) * 1000
        return coarse
    full = _search(world, p, start, end_x, beam=beam, deadline=began + budget_s,
                   max_ticks=max_ticks, stride=1)
    full.elapsed_ms = (time.perf_counter() - began) * 1000
    return full


#: Ground jumps considered every this many ticks in the first pass (16.7 ms).
COARSE_STRIDE = 4

#: Horizontal reach of a press, in ticks of travel: a jump's airtime (~104) plus margin.
#: With nothing gameplay-relevant that close ahead, a press cannot be needed.
PRESS_HORIZON_TICKS = 160


def _search(world: World, p: CubeParams, start: Cube, end_x: float, *, beam: int,
            deadline: float, max_ticks: int, stride: int) -> SolveResult:
    frontier = [Branch(start.copy())]
    overflowed = False
    furthest = start.x
    tick = 0
    deaths: list[Death] = []
    while frontier and tick < max_ticks:
        if time.perf_counter() > deadline:
            return SolveResult("inconclusive", furthest_x=furthest, reason="time budget")
        tick += 1
        seen: dict[tuple, Branch] = {}
        deaths = []
        for branch in frontier:
            cube = branch.cube
            reach = cube.x + PRESS_HORIZON_TICKS * p.dx[cube.speed] + 30
            may_press = (branch.release_at in (None, tick - 1)
                         and world.obstacle_between(cube.x - 30, reach))
            pre = cube.copy() if may_press else None
            death = advance(cube, world, p, tick, _actions(branch, tick - 1, False))
            if death is not None:
                deaths.append(death)
                continue
            options = [(cube, False)]
            can_press = pre is not None and (
                cube.touching_orbs or (cube.grounded and (tick - 1) % stride == 0))
            if can_press and advance(pre, world, p, tick,
                                     _actions(branch, tick - 1, True)) is None:
                options.append((pre, True))
            for state, did_press in options:
                child = Branch(
                    state,
                    branch.presses + ((tick - 1,) if did_press else ()),
                    tick if did_press else (None if branch.release_at == tick - 1
                                            else branch.release_at),
                )
                key = (round(state.x, 2), *state.key(), child.release_at is not None)
                kept = seen.get(key)
                # Fewer presses wins a tie: the same state reached more simply.
                if kept is None or len(child.presses) < len(kept.presses):
                    seen[key] = child
        frontier = list(seen.values())
        if frontier:
            furthest = max(furthest, max(b.cube.x for b in frontier))
        done = [b for b in frontier if b.cube.x >= end_x]
        if done:
            best = min(done, key=lambda b: len(b.presses))
            return SolveResult("possible", route=list(best.presses), furthest_x=furthest,
                               end_tick=tick, final=best.cube)
        if len(frontier) > beam:
            overflowed = True
            frontier.sort(key=lambda b: (len(b.presses), -b.cube.y))
            frontier = frontier[:beam]
    if frontier:  # ran out of ticks with branches alive
        return SolveResult("inconclusive", furthest_x=furthest, reason="tick limit")
    result = "inconclusive" if overflowed else "impossible"
    return SolveResult(result, furthest_x=furthest, frontier_deaths=deaths,
                       reason="beam overflow" if overflowed else None)


def _taps(presses: list[int]) -> dict[int, list[str]]:
    by_tick: dict[int, list[str]] = {}
    for t in sorted(presses):
        by_tick.setdefault(t + 1, []).append("release")
        by_tick.setdefault(t, []).append("press")
    # A release and a press on the same tick: release first, then press.
    return {t: sorted(a, key=lambda x: x != "release") for t, a in by_tick.items()}


def _reference(world: World, p: CubeParams, start: Cube, route: list[int],
               end_x: float, max_ticks: int) -> list[Cube]:
    """The route's state after every tick: states[k] is the state at tick k."""
    taps = _taps(route)
    cube = start.copy()
    states = [cube.copy()]
    for tick in range(1, max_ticks + 1):
        if advance(cube, world, p, tick, taps.get(tick - 1, ())) is not None:
            break
        states.append(cube.copy())
        if cube.x >= end_x:
            break
    return states


def _shift_survives(world: World, p: CubeParams, states: list[Cube], route: list[int],
                    index: int, moved: int, end_x: float) -> bool:
    """Does the route survive with press `index` moved to tick `moved`?

    Simulated from the reference state just before the earlier of the two ticks, and
    stopped as soon as the state matches the reference again: from there the future is
    the reference's, which is known to finish.
    """
    original = route[index]
    presses = [*route[:index], moved, *route[index + 1:]]
    begin = min(original, moved)
    if begin >= len(states):
        return False
    taps = _taps([t for t in presses if t >= begin])
    cube = states[begin].copy()
    settle = max(original, moved) + 2      # both taps fully released
    for tick in range(begin + 1, len(states) + 4 * 240):
        if advance(cube, world, p, tick, taps.get(tick - 1, ())) is not None:
            return False
        if cube.x >= end_x:
            return True
        if tick >= settle and tick < len(states):
            ref = states[tick]
            if abs(cube.x - ref.x) < 1e-6 and cube.key() == ref.key():
                return True
    return False


def windows(world: World, p: CubeParams, start: Cube, route: list[int], end_x: float,
            max_ticks: int) -> list[tuple[int, int]]:
    """For each press, the contiguous ticks it could move to with the others fixed."""
    states = _reference(world, p, start, route, end_x, max_ticks)
    out = []
    for i, tick in enumerate(route):
        others = set(route[:i] + route[i + 1:])
        lo = hi = tick
        for shift in range(1, WINDOW_REACH + 1):
            moved = tick - shift
            if moved < 0 or moved in others or not _shift_survives(
                    world, p, states, route, i, moved, end_x):
                break
            lo = moved
        for shift in range(1, WINDOW_REACH + 1):
            moved = tick + shift
            if moved in others or not _shift_survives(world, p, states, route, i, moved,
                                                      end_x):
                break
            hi = moved
        out.append((lo, hi))
    return out


def describe_route(world: World, p: CubeParams, start: Cube, solved: SolveResult,
                   end_x: float) -> dict[str, Any]:
    """The `verify_section` view of a solved route (LEVEL-DESIGN-SPEC.md 6.9)."""
    assert solved.end_tick is not None
    limit = solved.end_tick + 2
    spans = windows(world, p, start, solved.route, end_x, limit)
    trace = simulate(world, p, [{"tick": t, "action": a} for r in solved.route
                                for t, a in ((r, "press"), (r + 1, "release"))],
                     max_ticks=limit, until_x=end_x, start=start, start_tick=0)
    xs = dict(zip(trace.trace["tick"], trace.trace["x"], strict=True))
    route = []
    for tick, (lo, hi) in zip(solved.route, spans, strict=True):
        width = hi - lo + 1
        route.append({"tick": tick, "x": round(xs.get(tick, start.x), 1), "action": "press",
                      "window_ticks": [lo, hi], "window_ms": round(width * TICK_MS, 1)})
    widths = sorted(r["window_ms"] for r in route)
    seconds = solved.end_tick / 240.0
    return {
        "route": route,
        "difficulty": {
            "min_window_ms": widths[0] if widths else None,
            "p10_window_ms": widths[len(widths) // 10] if widths else None,
            "inputs_per_second": round(len(route) / seconds, 2) if seconds else 0.0,
            "frame_perfect": sum(1 for r in route if r["window_ticks"][0]
                                 == r["window_ticks"][1]),
        },
    }
