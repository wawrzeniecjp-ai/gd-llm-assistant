"""Fitting physics constants from replay traces (LEVEL-DESIGN-SPEC.md 7.4)."""

from __future__ import annotations

import json
from pathlib import Path

from gd_ai_assistant.design.calibration import (
    compact_trace,
    dx_per_tick,
    fit_jump,
    impulses,
    outcome_of,
    speed_change,
    takeoffs,
)

FIXTURES = Path(__file__).parent / "fixtures" / "replays"


def _synthetic(*, ticks: int = 300, press: int | None = 60, dx: float = 1.29825,
               dx_after: float | None = None, change_at: int = 10**9,
               vy0: float = 11.18, gravity: float = 0.216, scale: float = 0.225) -> dict:
    """A trace from the S-L3 model: what the fits must recover."""
    t = {k: [] for k in ("tick", "x", "y", "vy", "grounded")}
    x, y, vy, grounded = 0.0, 15.0, 0.0, True
    for tick in range(ticks):
        if tick:
            x += dx_after if (dx_after is not None and tick >= change_at) else dx
            if not grounded:
                vy -= gravity
                y += vy * scale
                if y <= 15.0:
                    y, vy, grounded = 15.0, 0.0, True
        if press is not None and tick == press + 1 and grounded:
            vy, grounded = vy0, False
        for key, value in zip(t, (tick, x, y, vy, int(grounded)), strict=True):
            t[key].append(value)
    return t


def test_fit_jump_recovers_the_model() -> None:
    fit = fit_jump(_synthetic())
    assert fit is not None
    assert fit.takeoff_tick == 61
    assert fit.jump_velocity == 11.18
    assert abs(fit.gravity - 0.216) < 1e-6
    assert abs(fit.scale - 0.225) < 1e-6
    assert fit.residual < 1e-6


def test_fit_jump_on_the_real_s_l3_trace() -> None:
    """The jump recorded in the game during S-L3 fits the model to within 0.01 units."""
    fixture = json.loads((FIXTURES / "sl3_jump.json").read_text(encoding="utf-8"))
    fit = fit_jump(fixture["trace"])
    assert fit is not None
    assert fit.takeoff_tick == 61
    assert fit.jump_velocity == 11.18
    assert abs(fit.gravity - 0.216) < 1e-4
    assert abs(fit.scale - 0.225) < 1e-3
    assert fit.residual < 0.05
    assert 63.0 < fit.apex < 64.5 and fit.airtime == 103


def test_speed_and_speed_change() -> None:
    trace = _synthetic(press=None, dx_after=2.0, change_at=100)
    assert abs(dx_per_tick(trace, 10, 90) - 1.29825) < 1e-9
    change = speed_change(trace)
    assert change is not None and change.tick == 100
    assert abs(change.after - 2.0) < 1e-9


def test_takeoffs_and_impulses() -> None:
    trace = _synthetic()
    assert takeoffs(trace) == [61]
    assert impulses(trace) == [(61, 11.18)]


def test_outcome_classification() -> None:
    assert outcome_of({"outcome": "death"}) == "death"
    on_block = {"outcome": "max_ticks", "trace": {"y": [15.0, 45.0], "grounded": [1, 1]}}
    assert outcome_of(on_block, landed_y=45.0) == "landed"
    assert outcome_of(on_block, landed_y=75.0) == "clear"


def test_compact_trace_keeps_events() -> None:
    trace = _synthetic()
    compact = compact_trace(trace)
    assert len(compact["tick"]) < len(trace["tick"]) / 3
    assert 61 in compact["tick"]          # the take-off
    assert compact["tick"][-1] == trace["tick"][-1]


def test_landings_report_an_extra_step() -> None:
    from gd_ai_assistant.design import calibration as cal
    """Slot 6, game ticks 512-526: the landing at 521 stepped 2.299 instead of 1.299."""
    xs = [18738.701, 18740.0, 18741.299, 18742.598, 18743.896, 18745.195, 18746.494,
          18747.793, 18749.092, 18751.391, 18752.689, 18753.988]
    trace = {"tick": list(range(512, 524)), "x": xs,
             "y": [88.8, 87.4, 85.9, 84.4, 82.8, 81.2, 79.5, 77.8, 76.0, 75.0, 75.0, 75.0],
             "grounded": [0] * 9 + [1, 1, 1]}
    (landing,) = cal.landings(trace)
    assert landing.tick == 521 and landing.y == 75.0
    assert abs(landing.extra - 1.0) < 0.01

    trace["x"] = [18738.701 + 1.299 * i for i in range(12)]
    (plain,) = cal.landings(trace)
    assert abs(plain.extra) < 0.01
