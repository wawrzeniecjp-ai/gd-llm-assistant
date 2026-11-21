"""Difficulty bands (LEVEL-DESIGN-SPEC.md 14.3) and their use by verify_section."""

from __future__ import annotations

import pytest

from gd_ai_assistant.design.bands import (
    TICK_MS,
    judge,
    load_bands,
    parse_difficulty,
    violations,
)
from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import CubeParams
from gd_ai_assistant.design.sim.verify import verify
from gd_ai_assistant.design.sim.world import world_from_objects

BANDS = load_bands()
ROLES = load_role_table()
PARAMS = CubeParams.from_constants(load_constants())


def _described(windows: list[float], rate: float = 1.0, mode: str = "cube",
               releases: dict[int, float] | None = None) -> dict:
    route = []
    for i, w in enumerate(windows):
        item = {"x": 300.0 + 100 * i, "window_ms": w, "mode": mode}
        if releases and i in releases:
            item.update(release_x=item["x"] + 40, release_window_ms=releases[i])
        route.append(item)
    return {"route": route, "difficulty": {"inputs_per_second": rate,
                                           "modes": {mode: 1.0},
                                           "p10_window_ms": min(windows, default=None)}}


def test_the_table_covers_every_difficulty_and_gets_harder() -> None:
    floors = [BANDS.base[d].min_window_ms for d in range(1, 11)]
    p10s = [BANDS.base[d].p10_window_ms for d in range(1, 11)]
    assert floors == sorted(floors, reverse=True)
    assert p10s == sorted(p10s, reverse=True)
    assert all(p >= f for p, f in zip(p10s, floors, strict=True))
    # Every floor is wider than one tick: a frame-perfect input is never in band.
    assert min(floors) > TICK_MS
    lows = [BANDS.base[d].inputs_per_second[0] for d in range(1, 11)]
    assert lows == sorted(lows)
    assert BANDS.provisional


def test_the_eval_cases_agree_with_the_bands() -> None:
    """The evals' soft ranges (easy >= 40 ms, medium 25-100, hard 12-60) were written
    first; a section built to its band must pass them."""
    assert BANDS.band(2).min_window_ms >= 40
    assert 25 <= BANDS.band(3).min_window_ms <= 100
    assert 12 <= BANDS.band(4).min_window_ms <= 60


def test_a_mode_scales_the_base_band() -> None:
    ship = BANDS.band(5, "ship")
    assert ship.min_window_ms < BANDS.band(5).min_window_ms
    assert ship.inputs_per_second[1] > BANDS.band(5).inputs_per_second[1]
    # An unknown mode falls back to the cube's band rather than failing.
    assert BANDS.band(5, "unknown") == BANDS.band(5)


def test_targets_mix_the_modes_by_time() -> None:
    cube = BANDS.targets(4, {"cube": 1.0})["inputs_per_second"]
    wave = BANDS.targets(4, {"wave": 1.0})["inputs_per_second"]
    half = BANDS.targets(4, {"cube": 0.5, "wave": 0.5})["inputs_per_second"]
    assert cube[1] < half[1] < wave[1]
    assert half[1] == pytest.approx((cube[1] + wave[1]) / 2, abs=0.01)


@pytest.mark.parametrize("value, level", [
    (5, 5), (0, 1), (14, 10), ("7", 7), ("easy", 2), ("Medium difficulty", 3),
    ("hard", 4), ("harder", 6), ("insane", 8), ("a demon", 10), ("tricky", None),
    (None, None), (True, None),
])
def test_difficulty_words_and_numbers(value, level) -> None:
    assert parse_difficulty(value) == level


def test_each_kind_of_violation() -> None:
    found = violations(_described([TICK_MS, 20.8, 100.0], rate=9.0), 4)
    codes = [v["code"] for v in found]
    assert codes == ["FRAME_PERFECT", "TIGHT_WINDOW", "TOO_DENSE"]
    assert found[1]["x"] == 400.0 and "at least 30" in found[1]["detail"]
    sparse = violations(_described([100.0], rate=0.1), 8)
    assert [v["code"] for v in sparse] == ["TOO_SPARSE"]


def test_a_hold_release_is_judged_too() -> None:
    found = violations(_described([80.0], rate=1.5, mode="ship", releases={0: 12.5}), 3)
    assert [(v["code"], v["x"]) for v in found] == [("TIGHT_WINDOW", 340.0)]
    assert "Release" in found[0]["detail"] and "(ship)" in found[0]["detail"]


def test_violations_are_capped_and_counted() -> None:
    found = violations(_described([20.0] * 12, rate=2.0), 4)
    assert sum(1 for v in found if v["code"] == "TIGHT_WINDOW" and "x" in v) == 8
    assert {"code": "TIGHT_WINDOW", "more": 4} in found


def test_judge_needs_a_difficulty() -> None:
    assert judge(_described([100.0]), None) is None
    assert judge(_described([100.0]), "tricky") is None
    easy = judge(_described([100.0], rate=1.0), "easy")
    assert easy is not None and easy["in_band"] and easy["violations"] == []
    assert easy["targets"]["name"] == "easy" and easy["targets"]["provisional"]


def test_density_alone_does_not_put_a_section_out_of_band() -> None:
    """TOO_DENSE is a warning (14.2); only errors and a low p10 fail the band."""
    judged = judge(_described([100.0], rate=9.0), 2)
    assert judged is not None and judged["in_band"]
    assert [v["code"] for v in judged["violations"]] == ["TOO_DENSE"]


# -- verify_section ----------------------------------------------------------------------


def _verify(objects: list[dict], args: dict) -> dict:
    return verify(world_from_objects(objects, ROLES), PARAMS, args)


def test_verify_judges_the_section_when_given_a_difficulty() -> None:
    spike = [{"id": 8, "x": 255, "y": 15}]
    plain = _verify(spike, {"x_range": [0, 600]})
    assert "targets" not in plain and "violations" not in plain
    judged = _verify(spike, {"x_range": [0, 600], "difficulty": "easy"})
    assert judged["result"] == "possible"
    assert judged["route"][0]["mode"] == "cube"
    assert judged["difficulty"]["modes"] == {"cube": 1.0}
    assert judged["targets"]["difficulty"] == 2
    assert judged["in_band"] is True


def test_a_section_is_measured_without_its_approach() -> None:
    """From the level start, a triple spike before the section must not stand for the
    section's tightest window, nor its presses dilute the section's density."""
    triple = [{"id": 8, "x": 255 + 30 * i, "y": 15} for i in range(3)]
    single = [{"id": 8, "x": 1515, "y": 15}]
    whole = _verify(triple + single, {"x_range": [0, 1800], "start_from": "explicit"})
    section = _verify(triple + single, {"x_range": [1200, 1800]})
    assert whole["result"] == section["result"] == "possible"
    assert section["approach_presses"] == 1 and len(section["route"]) == 1
    tightest_whole = whole["difficulty"]["min_window_ms"]
    assert section["difficulty"]["min_window_ms"] == section["route"][0]["window_ms"]
    assert section["difficulty"]["min_window_ms"] > tightest_whole
    # One press over the section's own time (600 units, ~1.9 s), not two over the run.
    assert section["difficulty"]["inputs_per_second"] == pytest.approx(0.52, abs=0.05)
    assert section["difficulty"]["inputs_per_second"] > whole["difficulty"]["inputs_per_second"]


def test_a_low_p10_alone_is_reported_not_judged() -> None:
    """With fewer than ten inputs the p10 window is the tightest one again: an out-of-band
    result with no violation to fix misled (ship-insert, 2026-11-22)."""
    described = _described([36.0], rate=1.0)
    described["difficulty"]["p10_window_ms"] = 36.0
    judged = judge(described, 3)
    assert judged is not None and judged["violations"] == []
    assert judged["targets"]["p10_window_ms"][0] > 36.0
    assert judged["in_band"] is True
