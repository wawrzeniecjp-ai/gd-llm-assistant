"""The beat grid and beat alignment (LEVEL-DESIGN-SPEC.md 8.3, 8.4.1, 14.2).

Beats live in song time: the first beat is at the plan's `music.offset_ms`, then one every
60000 / bpm ms, with `tempo_changes` switching the bpm from their `at_ms` (song time) on.
The timeline (`timeline.py`) turns song time into x.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from typing import Any

from .timeline import TICKS_PER_SECOND, Timeline

TICK_MS = 1000.0 / TICKS_PER_SECOND
#: A press window whose centre is further than this from the nearest subdivision is
#: off the beat (14.2).
BEAT_TOLERANCE_MS = 35.0
#: Beats listed at most in one `beat_grid` answer.
MAX_BEATS = 400


class MusicError(ValueError):
    """The plan's music cannot make a grid (no bpm, say)."""


@dataclass(frozen=True)
class Tempo:
    at_ms: float
    bpm: float
    beat_index: float      # beats counted from the first one up to `at_ms`


def _tempi(music: dict[str, Any]) -> list[Tempo]:
    bpm = music.get("bpm")
    if not isinstance(bpm, int | float) or bpm <= 0:
        raise MusicError("the plan has no bpm: ask the user for it (never guess from the "
                         "song's name), then set it with update_level_plan set_music")
    offset = float(music.get("offset_ms") or 0.0)
    tempi = [Tempo(offset, float(bpm), 0.0)]
    for change in sorted(music.get("tempo_changes") or [], key=lambda c: c["at_ms"]):
        at, new = float(change["at_ms"]), float(change["bpm"])
        last = tempi[-1]
        if at <= last.at_ms or new <= 0:
            continue
        tempi.append(Tempo(at, new, last.beat_index + (at - last.at_ms) * last.bpm / 60000.0))
    return tempi


def beat_times(music: dict[str, Any], start_ms: float, end_ms: float,
               subdivision: int = 1) -> list[tuple[float, float]]:
    """(song ms, beat number counted from 0 in steps of 1/subdivision) in the range."""
    tempi = _tempi(music)
    out: list[tuple[float, float]] = []
    step = 1.0 / max(1, subdivision)
    for i, tempo in enumerate(tempi):
        seg_end = tempi[i + 1].at_ms if i + 1 < len(tempi) else math.inf
        lo, hi = max(start_ms, tempo.at_ms), min(end_ms, seg_end)
        if lo > hi:
            continue
        ms_per_beat = 60000.0 / tempo.bpm
        first = math.ceil(((lo - tempo.at_ms) / ms_per_beat + tempo.beat_index) / step
                          - 1e-9) * step
        n = first
        while True:
            ms = tempo.at_ms + (n - tempo.beat_index) * ms_per_beat
            if ms > hi + 1e-9 or (ms >= seg_end - 1e-9 and seg_end != math.inf):
                break
            out.append((ms, round(n, 6)))
            if len(out) > MAX_BEATS * 4:
                break
            n += step
    return out


def beat_grid(music: dict[str, Any], timeline: Timeline, x_range: tuple[float, float],
              subdivision: int = 1) -> dict[str, Any]:
    """The `beat_grid` tool's answer: every beat (and subdivision) in the x range."""
    per_bar = int(music.get("beats_per_bar") or 4)
    x0, x1 = sorted(x_range)
    start = timeline.song_ms_at(max(x0, 0.0))
    end = timeline.song_ms_at(x1)
    beats = []
    for ms, n in beat_times(music, start, end, subdivision):
        whole = math.isclose(n, round(n), abs_tol=1e-6)
        beat_no = round(n) if whole else math.floor(n)
        strength = ("bar" if whole and beat_no % per_bar == 0 else
                    "beat" if whole else "sub")
        x = timeline.x_at_song_ms(ms)
        beats.append({"ms": round(ms, 1), "x": round(x, 1), "col": math.floor(x / 30),
                      "bar": beat_no // per_bar + 1, "beat": beat_no % per_bar + 1,
                      "sub": 0 if whole else round((n - beat_no) * subdivision),
                      "strength": strength})
    truncated = len(beats) > MAX_BEATS
    tempo = _tempi(music)[0].bpm
    ups = timeline.units_per_second(max(x0, 0.0))
    units_per_beat = ups * 60.0 / tempo
    result = {"beats": beats[:MAX_BEATS],
              "units_per_beat_at_start": round(units_per_beat, 1),
              "columns_per_beat_at_start": round(units_per_beat / 30.0, 2),
              "speed_at_start": timeline.speed_at(max(x0, 0.0))}
    if truncated:
        result["note"] = f"only the first {MAX_BEATS} beats; narrow x_range or the subdivision"
    return result


def nearest_offset_ms(music: dict[str, Any], song_ms: float, subdivision: int = 2) -> float:
    """Signed distance (ms) from `song_ms` to the nearest beat subdivision."""
    times = [ms for ms, _ in beat_times(music, song_ms - 4000, song_ms + 4000, subdivision)]
    if not times:
        return math.inf
    i = bisect_left(times, song_ms)
    near = [times[j] for j in (i - 1, i) if 0 <= j < len(times)]
    best = min(near, key=lambda t: abs(t - song_ms))
    return song_ms - best


def window_offset_ms(music: dict[str, Any], lo_ms: float, hi_ms: float,
                     subdivision: int = 2) -> tuple[float, bool]:
    """For a press window [lo_ms, hi_ms] (song time): the signed distance (ms) from the
    window's centre to the nearest beat subdivision (positive: the window is late, so the
    obstacle wants to move left), and whether that beat is inside the window at all."""
    lo_ms, hi_ms = min(lo_ms, hi_ms), max(lo_ms, hi_ms)
    centre = (lo_ms + hi_ms) / 2
    off = nearest_offset_ms(music, centre, subdivision)
    if not math.isfinite(off):
        return off, False
    return off, lo_ms - 1e-6 <= centre - off <= hi_ms + 1e-6


def press_windows_ms(timeline: Timeline, route: list[dict[str, Any]]
                     ) -> list[tuple[float, float]]:
    """Each route press's timing window in song ms, from `verify_section`'s route items
    (`tick`, `x` at that tick, `window_ticks`)."""
    out = []
    for item in route:
        base = timeline.song_ms_at(float(item["x"]))
        lo, hi = item.get("window_ticks") or (item["tick"], item["tick"])
        out.append((base + (lo - item["tick"]) * TICK_MS, base + (hi - item["tick"]) * TICK_MS))
    return out


def alignment(music: dict[str, Any], windows: list[tuple[float, float]],
              subdivision: int = 2,
              tolerance_ms: float = BEAT_TOLERANCE_MS) -> dict[str, Any]:
    """How many jumps can be pressed on the beat: the `beat_alignment` metric (14.2).

    A jump counts when a beat subdivision is inside its press window (song ms, from
    `press_windows_ms`) and within `tolerance_ms` of the window's centre: pressing on
    the beat then clears it with room to spare either side. The window, not the solver's
    one press: the solver presses at an edge. Not the whole window either: easy cube
    windows are ~100 ms, so "a beat anywhere in the window" passes three in four spikes
    placed at random at 120 bpm; the centre rule passes about one in four.
    """
    measured = [window_offset_ms(music, lo, hi, subdivision) for lo, hi in windows]
    offsets = [off for off, _ in measured]
    on = [off for off, inside in measured if inside and abs(off) <= tolerance_ms]
    return {"presses": len(offsets), "on_beat": len(on),
            "fraction": round(len(on) / len(offsets), 3) if offsets else None,
            "offsets_ms": [round(o, 1) for o in offsets], "tolerance_ms": tolerance_ms,
            "subdivision": subdivision}
