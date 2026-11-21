"""Difficulty bands (LEVEL-DESIGN-SPEC.md 14.3): what a section of a given difficulty may ask.

The numbers are data (`data/difficulty_bands.json`), provisional until tuned against rated
levels. This module turns them into a section's targets and its violations, with the codes
of 14.2: `FRAME_PERFECT` and `TIGHT_WINDOW` per input, `TOO_DENSE` and `TOO_SPARSE` for
the section.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

#: One tick, in ms. A window this narrow or narrower is frame-perfect.
TICK_MS = 1000.0 / 240.0

#: Words a user or a plan may use for a difficulty, on GD's star scale.
NAMED = {"auto": 1, "easy": 2, "normal": 3, "medium": 3, "hard": 4, "harder": 6,
         "insane": 8, "demon": 10, "extreme": 10}


@dataclass(frozen=True)
class Band:
    difficulty: int
    name: str
    min_window_ms: float
    p10_window_ms: float
    inputs_per_second: tuple[float, float | None]


@dataclass(frozen=True)
class Bands:
    provisional: bool
    base: dict[int, Band]
    modes: dict[str, dict[str, float]]

    def band(self, difficulty: int, mode: str = "cube") -> Band:
        """The band for one mode: the base window floors times the mode's `window`
        factor, and the density ceiling times its `density` factor. The density floor is
        not scaled: a ship flying straight for a while is not too sparse."""
        base = self.base[_clamp(difficulty)]
        factors = self.modes.get(mode, {"window": 1.0, "density": 1.0})
        lo, hi = base.inputs_per_second
        d = factors["density"]
        w = factors["window"]
        return Band(base.difficulty, base.name, round(base.min_window_ms * w, 1),
                    round(base.p10_window_ms * w, 1),
                    (lo, None if hi is None else round(hi * d, 2)))

    def targets(self, difficulty: int, modes: dict[str, float]) -> dict[str, Any]:
        """A section's targets, as `[min, max]` pairs (None: no bound).

        A section in several modes gets each mode's band weighted by the share of time
        spent in it. Single inputs are held to their own mode's floor (`violations`).
        """
        shares = {m: s for m, s in modes.items() if s > 0} or {"cube": 1.0}
        total = sum(shares.values())
        bands = {m: self.band(difficulty, m) for m in shares}

        def mix(get) -> float:
            return round(sum(get(bands[m]) * s for m, s in shares.items()) / total, 2)

        open_top = any(b.inputs_per_second[1] is None for b in bands.values())
        return {
            "difficulty": _clamp(difficulty),
            "name": self.base[_clamp(difficulty)].name,
            "min_window_ms": [mix(lambda b: b.min_window_ms), None],
            "p10_window_ms": [mix(lambda b: b.p10_window_ms), None],
            "inputs_per_second": [mix(lambda b: b.inputs_per_second[0]),
                                  None if open_top else
                                  mix(lambda b: b.inputs_per_second[1])],
            **({"provisional": True} if self.provisional else {}),
        }


def _clamp(difficulty: int) -> int:
    return min(10, max(1, int(difficulty)))


def parse_difficulty(value: Any) -> int | None:
    """1-10 from a number or a word ("easy", "hard", "7"); None if it is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _clamp(round(value))
    if isinstance(value, str):
        text = value.strip().lower()
        if text.isdigit():
            return _clamp(int(text))
        for word, level in NAMED.items():
            if word in text.split() or text == word:
                return level
    return None


def _default_path() -> Path:
    return Path(str(resources.files("gd_ai_assistant") / "data" / "difficulty_bands.json"))


@lru_cache(maxsize=4)
def load_bands(path: str | None = None) -> Bands:
    raw = json.loads((Path(path) if path else _default_path()).read_text(encoding="utf-8"))
    base = {}
    for key, entry in raw["base"].items():
        lo, hi = entry["inputs_per_second"]
        base[int(key)] = Band(int(key), entry["name"], float(entry["min_window_ms"]),
                              float(entry["p10_window_ms"]),
                              (float(lo), None if hi is None else float(hi)))
    if sorted(base) != list(range(1, 11)):
        raise ValueError("difficulty_bands.json must define difficulties 1 to 10")
    return Bands(bool(raw.get("provisional", False)), base,
                 {m: {"window": float(f["window"]), "density": float(f["density"])}
                  for m, f in raw["modes"].items()})


def _inputs(route: list[dict[str, Any]]):
    """(x, what, window_ms, mode) for every press and release in a described route."""
    for item in route:
        mode = item.get("mode", "cube")
        yield item["x"], "press", item["window_ms"], mode
        if "release_window_ms" in item:
            yield item.get("release_x", item["x"]), "release", item["release_window_ms"], mode


#: Listed violations of one code; the rest are counted.
MAX_LISTED = 8


def violations(described: dict[str, Any], difficulty: int,
               bands: Bands | None = None) -> list[dict[str, Any]]:
    """The 14.2 violations of a described route against a difficulty's band."""
    bands = bands or load_bands()
    level = _clamp(difficulty)
    out: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    def add(item: dict[str, Any]) -> None:
        counts[item["code"]] = counts.get(item["code"], 0) + 1
        if counts[item["code"]] <= MAX_LISTED:
            out.append(item)

    for x, what, window, mode in _inputs(described.get("route", [])):
        if window <= TICK_MS + 0.05:
            add({"code": "FRAME_PERFECT", "severity": "error", "x": x,
                 "detail": f"{what.capitalize()} at x={x:g} has a one-tick window "
                           f"({window:g} ms)."})
            continue
        floor = bands.band(level, mode).min_window_ms
        if window < floor:
            add({"code": "TIGHT_WINDOW", "severity": "error", "x": x,
                 "detail": f"{what.capitalize()} at x={x:g} has a {window:g} ms window; "
                           f"difficulty {level} ({mode}) asks for at least {floor:g} ms."})

    metrics = described.get("difficulty", {})
    rate = metrics.get("inputs_per_second")
    if rate is not None:
        lo, hi = bands.targets(level, metrics.get("modes") or {"cube": 1.0}
                               )["inputs_per_second"]
        if hi is not None and rate > hi:
            add({"code": "TOO_DENSE", "severity": "warn",
                 "detail": f"{rate:g} inputs/s; difficulty {level} asks for at most {hi:g}."})
        elif rate < lo:
            add({"code": "TOO_SPARSE", "severity": "warn",
                 "detail": f"{rate:g} inputs/s; difficulty {level} asks for at least {lo:g}."})

    for code, n in counts.items():
        if n > MAX_LISTED:
            out.append({"code": code, "more": n - MAX_LISTED})
    return out


def judge(described: dict[str, Any], difficulty: Any,
          bands: Bands | None = None) -> dict[str, Any] | None:
    """`targets`, `violations` and `in_band` for a described route, or None without a
    usable difficulty.

    `in_band` is false on any error violation, and only then, so it always comes with
    something to fix. The p10 target is reported, not judged: with fewer than ten inputs
    the p10 window is the tightest one again, and a section measured out of band for it
    would show nothing to fix (2026-11-22, ship-insert)."""
    level = parse_difficulty(difficulty)
    if level is None:
        return None
    bands = bands or load_bands()
    metrics = described.get("difficulty", {})
    targets = bands.targets(level, metrics.get("modes") or {"cube": 1.0})
    found = violations(described, level, bands)
    in_band = not any(v.get("severity") == "error" for v in found)
    return {"targets": targets, "violations": found, "in_band": in_band}
