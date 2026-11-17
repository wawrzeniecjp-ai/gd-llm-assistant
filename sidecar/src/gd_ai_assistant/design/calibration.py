"""Reading physics constants off replay traces (LEVEL-DESIGN-SPEC.md 7.4).

Pure functions over the columnar traces `run_replay` returns, so they can be tested
offline on synthetic traces. `scripts/calibrate.py` runs the probes in the game and
calls these.

The cube's vertical motion, as S-L3 measured it, per tick:

    vy  <- vy - gravity          (gravity in vy units per tick)
    y   <- y + vy * scale        (scale: units of y per vy unit per tick)

with `vy` set to the jump velocity on take-off. The fits below assume nothing beyond
that shape and report how well it holds (`residual`), so a probe that breaks the model
says so instead of producing a confident wrong number.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

Trace = dict[str, list[Any]]

#: A change in vy larger than this between two ticks is an impulse (jump, orb, pad),
#: not gravity, which moves vy by ~0.2 per tick.
IMPULSE_THRESHOLD = 1.0


def _rows(trace: Trace) -> list[dict[str, Any]]:
    keys = [k for k in ("tick", "x", "y", "vy", "grounded", "speed") if k in trace]
    return [dict(zip(keys, values, strict=True))
            for values in zip(*(trace[k] for k in keys), strict=True)]


def dx_per_tick(trace: Trace, first: int, last: int) -> float | None:
    """Average horizontal step between two ticks (inclusive), or None with no data.

    Distance over ticks, not the median of single steps: the mod rounds positions to
    3 decimals, so a single step reads 1.298 or 1.299 when the true value is 1.29825.
    Over a few hundred ticks the rounding cancels out.
    """
    rows = [r for r in _rows(trace) if first <= r["tick"] <= last]
    if len(rows) < 2 or rows[-1]["tick"] == rows[0]["tick"]:
        return None
    return (rows[-1]["x"] - rows[0]["x"]) / (rows[-1]["tick"] - rows[0]["tick"])


@dataclass(frozen=True)
class SpeedChange:
    tick: int            # first tick moving at the new speed
    before: float        # dx per tick
    after: float
    x_at_change: float   # player x on that tick


def speed_change(trace: Trace, tolerance: float = 0.01) -> SpeedChange | None:
    """The first tick where the horizontal step changes and then stays changed."""
    rows = _rows(trace)
    steps = [(b["tick"], b["x"] - a["x"], b["x"]) for a, b in zip(rows, rows[1:], strict=False)
             if b["tick"] == a["tick"] + 1]
    if len(steps) < 4:
        return None
    base = steps[1][1]  # skip the very first step out of the start
    for i, (tick, dx, x) in enumerate(steps[2:], start=2):
        if abs(dx - base) > tolerance:
            # Averaged over everything after the change (see dx_per_tick on rounding).
            tail = steps[i:]
            after = (sum(s[1] for s in tail) / len(tail)) if tail else dx
            before = (x - dx - steps[0][2]) / max(1, tick - 1 - steps[0][0]) if i > 1 else base
            return SpeedChange(tick=tick, before=before, after=after, x_at_change=x)
    return None


def takeoffs(trace: Trace) -> list[int]:
    """Ticks where the player leaves the ground."""
    rows = _rows(trace)
    return [b["tick"] for a, b in zip(rows, rows[1:], strict=False)
            if a["grounded"] and not b["grounded"]]


def impulses(trace: Trace) -> list[tuple[int, float]]:
    """(tick, new vy) wherever vy jumps upward by more than gravity could explain."""
    rows = _rows(trace)
    return [(b["tick"], b["vy"]) for a, b in zip(rows, rows[1:], strict=False)
            if b["vy"] - a["vy"] > IMPULSE_THRESHOLD and not b["grounded"]]


@dataclass(frozen=True)
class JumpFit:
    takeoff_tick: int
    jump_velocity: float
    gravity: float
    scale: float
    apex: float           # height above the take-off y
    airtime: int          # ticks from take-off to landing
    residual: float       # worst |predicted - actual| y over the airborne span


def fit_jump(trace: Trace, after_tick: int = 0) -> JumpFit | None:
    """Fit the cube's vertical model to the first jump at or after `after_tick`."""
    rows = [r for r in _rows(trace) if r["tick"] >= after_tick]
    # The first airborne stretch long enough to fit. A lone airborne sample is not a
    # jump: the tick-0 sample, taken before the first physics step, reads as airborne.
    start = end = None
    i = 0
    while i < len(rows):
        if rows[i]["grounded"]:
            i += 1
            continue
        j = i
        while j + 1 < len(rows) and not rows[j + 1]["grounded"]:
            j += 1
        if j - i + 1 >= 5:
            start, end = i, j
            break
        i = j + 1
    if start is None or end is None:
        return None
    air = rows[start:end + 1]
    gravity = statistics.median(a["vy"] - b["vy"] for a, b in zip(air, air[1:], strict=False))
    ratios = [(b["y"] - a["y"]) / b["vy"] for a, b in zip(air, air[1:], strict=False)
              if abs(b["vy"]) > 0.5]
    scale = statistics.median(ratios)
    # Predict y from the take-off sample with the fitted model and measure the worst miss.
    y, vy, worst = air[0]["y"], air[0]["vy"], 0.0
    for row in air[1:]:
        vy -= gravity
        y += vy * scale
        worst = max(worst, abs(y - row["y"]))
    base = rows[start - 1]["y"] if start > 0 else air[0]["y"]
    landing = rows[end + 1]["tick"] if end + 1 < len(rows) else air[-1]["tick"] + 1
    return JumpFit(
        takeoff_tick=air[0]["tick"],
        jump_velocity=air[0]["vy"],
        gravity=round(gravity, 6),
        scale=round(scale, 6),
        apex=round(max(r["y"] for r in air) - base, 3),
        airtime=landing - air[0]["tick"],
        residual=round(worst, 4),
    )


@dataclass(frozen=True)
class Landing:
    tick: int
    x: float
    y: float
    step: float      # the x step into the landing sample
    extra: float     # how much longer that step was than the steps just before it


def landings(trace: Trace) -> list[Landing]:
    """Every landing (airborne, then grounded on the next tick) with its x step.

    P10: on one stair in the M2 sections the game moved the cube one extra unit forward
    on the landing tick. `extra` is that step minus the median of the five before it.
    """
    rows = _rows(trace)
    out = []
    for k in range(6, len(rows)):
        now, before = rows[k], rows[k - 1]
        if not now.get("grounded") or before.get("grounded"):
            continue
        if now["tick"] - before["tick"] != 1:
            continue
        window = rows[k - 6:k]
        if any(b["tick"] - a["tick"] != 1 for a, b in zip(window, window[1:], strict=False)):
            continue
        normal = statistics.median(b["x"] - a["x"] for a, b in zip(window, window[1:],
                                                                    strict=False))
        step = now["x"] - before["x"]
        out.append(Landing(int(now["tick"]), now["x"], now["y"], round(step, 3),
                           round(step - normal, 3)))
    return out


def outcome_of(result: dict[str, Any], *, landed_y: float | None = None) -> str:
    """Classify a probe run: 'death', 'landed' (stood at `landed_y` at some point), or
    'clear'."""
    if result.get("outcome") == "death":
        return "death"
    trace = result.get("trace") or {}
    if landed_y is not None:
        for y, grounded in zip(trace.get("y", []), trace.get("grounded", []), strict=False):
            if grounded and abs(y - landed_y) < 1.0:
                return "landed"
    return "clear"


#: Player-state columns beyond the cube's, kept by `compact_trace(moded=True)`.
MODE_COLUMNS = ("mode", "gravity", "mini")


def compact_trace(trace: Trace, every: int = 4, *, moded: bool = False) -> Trace:
    """Every `every`-th sample plus every tick where grounded or vy changes sharply: the
    golden form (LEVEL-DESIGN-SPEC.md 6.7, 7.5). Rounded to keep fixtures small.

    `moded` (Sim-2 probes) also keeps `mode`, `gravity` and `mini`, and every tick where
    one of them changes."""
    rows = _rows(trace)
    extra = [k for k in MODE_COLUMNS if moded and k in trace]
    for row, values in zip(rows, zip(*(trace[k] for k in extra), strict=True)
                           if extra else ((),) * len(rows), strict=True):
        row.update(zip(extra, values, strict=True))
    keep: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        prev = rows[i - 1] if i else None
        event = prev is not None and (row["grounded"] != prev["grounded"]
                                      or abs(row["vy"] - prev["vy"]) > IMPULSE_THRESHOLD
                                      or any(row[k] != prev[k] for k in extra))
        if i % every == 0 or event or i == len(rows) - 1:
            keep.append(row)
    out: Trace = {k: [] for k in ("tick", "x", "y", "vy", "grounded", *extra)}
    for row in keep:
        out["tick"].append(row["tick"])
        out["x"].append(round(row["x"], 3))
        out["y"].append(round(row["y"], 3))
        out["vy"].append(round(row["vy"], 4))
        out["grounded"].append(int(row["grounded"]))
        for k in extra:
            out[k].append(row[k])
    return out
