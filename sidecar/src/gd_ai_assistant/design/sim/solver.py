"""Finding a route through a section, and how forgiving it is (LEVEL-DESIGN-SPEC.md 6.8).

Breadth-first by tick over decision points, with deduplication. A branch only has a
choice when an input would do something:

- the cube and the ball: on the ground (a jump, a flip) or touching an unused orb; the
  press is a tap, released on the next tick;
- the ufo: any tick (it jumps in the air too), a tap;
- the ship and the wave: any tick, start holding or let go; a hold lasts until a later
  decision releases it.

Between decision points the state advances deterministically. A route is its press
ticks and, for each, the tick it is released (a tap: the next one).

Outcomes: `possible` (a route reaches the end alive), `impossible` (every branch died and
nothing was cut), `inconclusive` (the beam overflowed or the time budget ran out, so a
route may exist), `unverifiable` (the span holds something the simulator does not know).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .cube import HALF, Cube, CubeParams, Death, advance, simulate
from .world import World

DEFAULT_BEAM = 2000
DEFAULT_BUDGET_S = 10.0
WINDOW_REACH = 24            # ticks either side (100 ms), LEVEL-DESIGN-SPEC.md 6.8.3
TICK_MS = 1000.0 / 240.0
#: Modes played by holding: the search presses and releases on decision ticks.
HOLD_MODES = frozenset({"ship", "wave"})
#: Modes whose press does something in the air.
AIR_MODES = frozenset({"ship", "wave", "ufo"})
#: In the air modes, decisions every this many ticks in the coarse and the full pass.
FLIGHT_STRIDE = {"coarse": 8, "full": 2}
#: Flying states closer than this (y, vy) count as one in the search (the cube's own key
#: is finer: 0.5 and 0.05).
FLIGHT_Y_STEP = 2.0
FLIGHT_VY_STEP = 0.5
#: The coarse pass keeps at most this many branches, spread over heights (see `_prune`);
#: when it finds nothing, the full pass with the whole beam decides.
COARSE_BEAM = 300


@dataclass
class Branch:
    cube: Cube
    presses: tuple[int, ...] = ()
    #: The release tick of each press; an open hold has none yet.
    releases: tuple[int, ...] = ()
    release_at: int | None = None     # tick whose commands release the current tap

    @property
    def holding(self) -> bool:
        return len(self.releases) < len(self.presses)


@dataclass
class SolveResult:
    result: str
    route: list[int] = field(default_factory=list)       # press ticks
    #: The release tick of each press (a tap: press + 1).
    releases: list[int] = field(default_factory=list)
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

    A coarse pass first (decisions only every `COARSE_STRIDE` ticks), which finds the
    usual route quickly; the window search then reports how exact it really is. Only
    when that fails does the full search run, and only the full search may answer
    `impossible`, so an input that needs one exact tick is never missed.
    """
    began = time.perf_counter()
    if max_ticks is None:
        slowest = min(p.dx.values())
        max_ticks = int((end_x - start.x) / slowest) + 2 * 240
    coarse = _search(world, p, start, end_x, beam=min(beam, COARSE_BEAM),
                     deadline=began + budget_s,
                     max_ticks=max_ticks, stride=COARSE_STRIDE)
    if coarse.result == "possible" or coarse.reason == "time budget":
        coarse.elapsed_ms = (time.perf_counter() - began) * 1000
        return coarse
    full = _search(world, p, start, end_x, beam=beam, deadline=began + budget_s,
                   max_ticks=max_ticks, stride=1)
    full.elapsed_ms = (time.perf_counter() - began) * 1000
    return full


#: Decisions considered every this many ticks in the first pass (16.7 ms).
COARSE_STRIDE = 4

#: Horizontal reach of a press, in ticks of travel: a jump's airtime (~104) plus margin.
#: With nothing gameplay-relevant that close ahead, a press cannot be needed.
PRESS_HORIZON_TICKS = 160


def _search_key(cube: Cube) -> tuple:
    """What the search deduplicates on: the cube's key, coarser while flying."""
    key = cube.key()
    if cube.mode in AIR_MODES:
        key = (round(cube.y / FLIGHT_Y_STEP), round(cube.vy / FLIGHT_VY_STEP), *key[2:])
    return key


def _search(world: World, p: CubeParams, start: Cube, end_x: float, *, beam: int,
            deadline: float, max_ticks: int, stride: int) -> SolveResult:
    flight_stride = FLIGHT_STRIDE["coarse" if stride > 1 else "full"]
    frontier = [Branch(start.copy())]
    overflowed = False
    furthest = start.x
    tick = 0
    deaths: list[Death] = []
    # Branches that met something the simulator does not know (Sim-2's uncalibrated
    # contacts): if no route avoids them, the answer is unverifiable, not impossible.
    blocked: list[Death] = []
    while frontier and tick < max_ticks:
        if time.perf_counter() > deadline:
            return SolveResult("inconclusive", furthest_x=furthest, reason="time budget")
        tick += 1
        decide = (tick - 1) % stride == 0
        fly = (tick - 1) % flight_stride == 0
        seen: dict[tuple, Branch] = {}
        deaths = []
        # Most branches share an x (same speed, same ticks): look the world up once per x.
        lookups: dict[tuple[float, str], tuple[list, bool]] = {}
        for branch in frontier:
            cube = branch.cube
            spot = (cube.x, cube.speed)
            lookup = lookups.get(spot)
            if lookup is None:
                dx = p.dx[cube.speed]
                lookup = lookups[spot] = (
                    world.near(cube.x - HALF, cube.x + dx + HALF),
                    world.obstacle_between(cube.x - 30, cube.x + PRESS_HORIZON_TICKS * dx + 30))
            near, obstacle_ahead = lookup
            holding = branch.holding
            # A hold may be let go at any decision tick (also after leaving its mode).
            may_release = holding and fly
            may_press = (not holding and branch.release_at in (None, tick - 1)
                         and obstacle_ahead)
            pre = cube.copy() if may_press or may_release else None
            death = advance(cube, world, p, tick, _actions(branch, tick - 1, False), near)
            if death is not None:
                (blocked if death.cause == "unverifiable" else deaths).append(death)
                continue
            children = [Branch(cube, branch.presses, branch.releases,
                               None if branch.release_at == tick - 1 else branch.release_at)]
            if pre is not None:
                child = _decide(world, p, branch, pre, cube, tick, near, decide, fly,
                                may_release)
                if child is not None:
                    children.append(child)
            for child in children:
                key = (round(child.cube.x, 2), *_search_key(child.cube),
                       child.release_at is not None, child.holding)
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
            releases = list(best.releases)
            if best.holding:
                releases.append(tick)
            return SolveResult("possible", route=list(best.presses), releases=releases,
                               furthest_x=furthest, end_tick=tick, final=best.cube)
        if len(frontier) > beam:
            overflowed = True
            frontier = _prune(frontier, beam)
    if blocked:
        first = min(blocked, key=lambda d: d.x)
        return SolveResult("unverifiable", furthest_x=furthest, frontier_deaths=[first],
                           reason=first.reason)
    if frontier:  # ran out of ticks with branches alive
        return SolveResult("inconclusive", furthest_x=furthest, reason="tick limit")
    result = "inconclusive" if overflowed else "impossible"
    return SolveResult(result, furthest_x=furthest, frontier_deaths=deaths,
                       reason="beam overflow" if overflowed else None)


def _prune(frontier: list[Branch], beam: int) -> list[Branch]:
    """The `beam` branches to keep: the simplest (fewest presses, then highest) in each
    band of heights, mode and hold state first, then the simplest of the rest. Keeping
    only the simplest overall left a flying frontier all at one height."""
    frontier.sort(key=lambda b: (len(b.presses), -b.cube.y))
    firsts: dict[tuple, Branch] = {}
    for b in frontier:
        firsts.setdefault((b.cube.mode, b.holding, b.cube.gravity, round(b.cube.y / 15),
                           b.cube.vy > 0), b)
    kept = list(firsts.values())[:beam]
    chosen = {id(b) for b in kept}
    kept += [b for b in frontier if id(b) not in chosen][:beam - len(kept)]
    return kept


def _decide(world: World, p: CubeParams, branch: Branch, pre: Cube, idle: Cube, tick: int,
            near: list, decide: bool, fly: bool, may_release: bool) -> Branch | None:
    """The branch that acts this tick, if acting would do something and survives. `idle`
    is the same tick without acting: its mode and contacts say what a press would do
    (the commands run after the move and contacts)."""
    t = tick - 1
    if may_release:
        if advance(pre, world, p, tick, [*_actions(branch, t, False), "release"], near):
            return None
        return Branch(pre, branch.presses, (*branch.releases, t), branch.release_at)
    hold = idle.mode in HOLD_MODES and fly
    tap = (idle.touching_orbs or (decide and idle.grounded)
           or (fly and idle.mode == "ufo"))
    if not (hold or tap):
        return None
    if advance(pre, world, p, tick, _actions(branch, t, True), near) is not None:
        return None
    if hold:
        return Branch(pre, (*branch.presses, t), branch.releases, None)
    return Branch(pre, (*branch.presses, t), (*branch.releases, tick), tick)


def tap_releases(route: Sequence[int]) -> list[int]:
    return [t + 1 for t in route]


def commands(route: Sequence[int], releases: Sequence[int] | None = None,
             since: int = 0) -> dict[int, list[str]]:
    """Each tick's commands: presses, and their releases (a tap's on the next tick).
    Only ticks from `since` on."""
    releases = tap_releases(route) if releases is None else releases
    by_tick: dict[int, list[str]] = {}
    for t, r in zip(route, releases, strict=True):
        if r >= since:
            by_tick.setdefault(r, []).append("release")
        if t >= since:
            by_tick.setdefault(t, []).append("press")
    # A release and a press on the same tick: release first, then press.
    return {t: sorted(a, key=lambda x: x != "release") for t, a in by_tick.items()}


def input_events(route: Sequence[int], releases: Sequence[int] | None = None,
                 offset: int = 0) -> list[dict[str, Any]]:
    """The route as `simulate` / `run_replay` inputs, ticks shifted by `offset`."""
    by_tick = commands(route, releases)
    return [{"tick": t + offset, "action": a} for t in sorted(by_tick) for a in by_tick[t]]


def _reference(world: World, p: CubeParams, start: Cube, route: Sequence[int],
               releases: Sequence[int], end_x: float, max_ticks: int) -> list[Cube]:
    """The route's state after every tick: states[k] is the state at tick k."""
    by_tick = commands(route, releases)
    cube = start.copy()
    states = [cube.copy()]
    for tick in range(1, max_ticks + 1):
        if advance(cube, world, p, tick, by_tick.get(tick - 1, ())) is not None:
            break
        states.append(cube.copy())
        if cube.x >= end_x:
            break
    return states


def _survives(world: World, p: CubeParams, states: list[Cube], route: list[int],
              releases: list[int], changed: tuple[int, int], end_x: float) -> bool:
    """Does the route (one input moved: `changed` is its old and new tick) still finish?

    Simulated from the reference state just before the earlier of the two ticks, and
    stopped as soon as the state matches the reference again: from there the future is
    the reference's, which is known to finish.
    """
    begin = min(changed)
    if begin >= len(states):
        return False
    by_tick = commands(route, releases, since=begin)
    cube = states[begin].copy()
    settle = max(changed) + 2      # both versions of the input fully played
    for tick in range(begin + 1, len(states) + 4 * 240):
        if advance(cube, world, p, tick, by_tick.get(tick - 1, ())) is not None:
            return False
        if cube.x >= end_x:
            return True
        if tick >= settle and tick < len(states):
            ref = states[tick]
            if abs(cube.x - ref.x) < 1e-6 and cube.key() == ref.key():
                return True
    return False


def move_input(route: list[int], releases: list[int], i: int, part: str,
           tick: int) -> tuple[list[int], list[int]] | None:
    """The route with press `i`'s `part` ("tap": the whole tap, "press" or "release" of a
    hold) at `tick`, or None if that would cross a neighbouring input."""
    route, releases = list(route), list(releases)
    lo = releases[i - 1] if i > 0 else 0
    hi = route[i + 1] if i + 1 < len(route) else None
    if part == "tap":
        route[i], releases[i] = tick, tick + 1
        ok = tick >= lo and (hi is None or tick + 1 <= hi)
    elif part == "press":
        route[i] = tick
        ok = lo <= tick < releases[i]
    else:
        releases[i] = tick
        ok = route[i] < tick and (hi is None or tick <= hi)
    return (route, releases) if ok and tick >= 0 else None


def _window(world: World, p: CubeParams, states: list[Cube], route: list[int],
            releases: list[int], i: int, part: str, end_x: float) -> tuple[int, int]:
    """The contiguous ticks one input could move to with the others fixed."""
    tick = releases[i] if part == "release" else route[i]
    lo = hi = tick
    for sign in (-1, 1):
        for shift in range(1, WINDOW_REACH + 1):
            moved = tick + sign * shift
            candidate = move_input(route, releases, i, part, moved)
            if candidate is None or not _survives(world, p, states, *candidate,
                                                  (tick, moved), end_x):
                break
            lo, hi = min(lo, moved), max(hi, moved)
    return lo, hi


def _is_tap(route: Sequence[int], releases: Sequence[int], i: int) -> bool:
    return releases[i] == route[i] + 1


def windows(world: World, p: CubeParams, start: Cube, route: list[int], end_x: float,
            max_ticks: int, releases: list[int] | None = None) -> list[tuple[int, int]]:
    """For each press, the contiguous ticks it could move to with the others fixed (a
    tap moves whole; a hold's press moves with its release fixed)."""
    releases = tap_releases(route) if releases is None else releases
    states = _reference(world, p, start, route, releases, end_x, max_ticks)
    return [_window(world, p, states, route, releases, i,
                    "tap" if _is_tap(route, releases, i) else "press", end_x)
            for i in range(len(route))]


def release_windows(world: World, p: CubeParams, start: Cube, route: list[int],
                    releases: list[int], end_x: float,
                    max_ticks: int) -> list[tuple[int, int] | None]:
    """For each hold, the ticks its release could move to; None for a tap."""
    states = _reference(world, p, start, route, releases, end_x, max_ticks)
    return [None if _is_tap(route, releases, i)
            else _window(world, p, states, route, releases, i, "release", end_x)
            for i in range(len(route))]


def centre_inputs(world: World, p: CubeParams, start: Cube, route: list[int],
                  releases: list[int] | None, end_x: float,
                  max_ticks: int) -> tuple[list[int], list[int]]:
    """The route with every input moved to the middle of its timing window.

    The search finds the latest input that works, which sits on the window's last tick:
    the one where the simulator being a tick or a unit off matters most. Inputs are
    centred in order, each window measured with the earlier ones already centred, so the
    route still finishes after every move (a window only holds ticks that do).
    """
    route = list(route)
    releases = tap_releases(route) if releases is None else list(releases)
    for i in range(len(route)):
        parts = ("tap",) if _is_tap(route, releases, i) else ("press", "release")
        for part in parts:
            states = _reference(world, p, start, route, releases, end_x, max_ticks)
            lo, hi = _window(world, p, states, route, releases, i, part, end_x)
            moved = move_input(route, releases, i, part, (lo + hi) // 2)
            assert moved is not None
            route, releases = moved
    return route, releases


def centre_route(world: World, p: CubeParams, start: Cube, route: list[int], end_x: float,
                 max_ticks: int) -> list[int]:
    """`centre_inputs` for a route of taps."""
    return centre_inputs(world, p, start, route, None, end_x, max_ticks)[0]


def describe_route(world: World, p: CubeParams, start: Cube, solved: SolveResult,
                   end_x: float) -> dict[str, Any]:
    """The `verify_section` view of a solved route (LEVEL-DESIGN-SPEC.md 6.9)."""
    assert solved.end_tick is not None
    limit = solved.end_tick + 2
    releases = solved.releases or tap_releases(solved.route)
    spans = windows(world, p, start, solved.route, end_x, limit, releases)
    release_spans = release_windows(world, p, start, solved.route, releases, end_x, limit)
    trace = simulate(world, p, input_events(solved.route, releases),
                     max_ticks=limit, until_x=end_x, start=start, start_tick=0)
    xs = dict(zip(trace.trace["tick"], trace.trace["x"], strict=True))
    route = []
    widths = []
    for tick, release, (lo, hi), held in zip(solved.route, releases, spans, release_spans,
                                             strict=True):
        width = round((hi - lo + 1) * TICK_MS, 1)
        widths.append(width)
        item = {"tick": tick, "x": round(xs.get(tick, start.x), 1), "action": "press",
                "window_ticks": [lo, hi], "window_ms": width}
        if held is not None:
            rlo, rhi = held
            release_ms = round((rhi - rlo + 1) * TICK_MS, 1)
            widths.append(release_ms)
            item.update(action="hold", release_tick=release,
                        release_x=round(xs.get(release, start.x), 1),
                        hold_ms=round((release - tick) * TICK_MS, 1),
                        release_window_ticks=[rlo, rhi], release_window_ms=release_ms)
        route.append(item)
    widths.sort()
    seconds = solved.end_tick / 240.0
    exact = sum(1 for r in route if r["window_ticks"][0] == r["window_ticks"][1])
    exact += sum(1 for r in route if "release_window_ticks" in r
                 and r["release_window_ticks"][0] == r["release_window_ticks"][1])
    return {
        "route": route,
        "difficulty": {
            "min_window_ms": widths[0] if widths else None,
            "p10_window_ms": widths[len(widths) // 10] if widths else None,
            "inputs_per_second": round(len(widths) / seconds, 2) if seconds else 0.0,
            "frame_perfect": exact,
        },
    }
