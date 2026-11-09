"""The M2 test-section drafts (scripts/m2_sections.py) are what they claim to be.

These are the starting points placed by scripts/build_sections.py. If a change to the
simulator flips one, the draft or the change needs a second look before anything is
placed in the game.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_sections import payloads, verify_args  # noqa: E402
from m2_sections import SECTIONS  # noqa: E402

from gd_ai_assistant.design.constants import load_constants  # noqa: E402
from gd_ai_assistant.design.coords import Grid  # noqa: E402
from gd_ai_assistant.design.roles import load_role_table  # noqa: E402
from gd_ai_assistant.design.sim.cube import CubeParams  # noqa: E402
from gd_ai_assistant.design.sim.verify import verify  # noqa: E402
from gd_ai_assistant.design.sim.world import world_from_objects  # noqa: E402


@pytest.fixture(scope="module")
def level():
    grid = Grid.from_constants()
    objects = [p for s in SECTIONS for p in payloads(s, grid)]
    return (world_from_objects(objects, load_role_table()),
            CubeParams.from_constants(load_constants()))


def test_twenty_slots_ten_each_way() -> None:
    assert [s.slot for s in SECTIONS] == list(range(1, 21))
    assert sum(s.expected == "possible" for s in SECTIONS) == 10


def test_runway_is_clear() -> None:
    """Columns 0-9 hold at most the start position and a speed portal."""
    for section in SECTIONS:
        runway = {c for row in section.rows for c in row[:10]}
        assert runway <= set(".@123456"), (section.slot, runway)


def test_every_runway_sets_its_speed() -> None:
    """A start position starts at 1x, so each slot sets its speed rather than inherit it."""
    for section in SECTIONS:
        portals = [c for row in section.rows for c in row[:10] if c in "12345"]
        assert len(portals) == 1, (section.slot, portals)
        if section.slot not in (8, 9, 18, 19):
            assert portals == ["2"], section.slot


@pytest.mark.parametrize("section", SECTIONS, ids=lambda s: f"slot{s.slot}")
def test_draft_classified_as_intended(level, section) -> None:
    world, params = level
    assert verify(world, params, verify_args(section.slot))["result"] == section.expected


@pytest.mark.parametrize("run", ["main", "press2_lo1"])
def test_slot10_matches_the_game(level, run) -> None:
    """Slot 10 as the game played it (playtest 2026-11-09), tick by tick.

    Pins three things the game showed: x is a 32-bit float (half a unit ahead after 1000
    ticks otherwise), falling stops at vy -15, and a cube whose feet are 8.72 below a
    block's top still lands on it (the press-2 lo-1 run, game tick 718).
    """
    import json

    from gd_ai_assistant.design.sim.cube import Cube, simulate

    world, params = level
    fixture = Path(__file__).parent / "fixtures" / "replays" / "playtest_slot10.json"
    data = next(r for r in json.loads(fixture.read_text(encoding="utf-8"))["runs"]
                if r["name"] == run)
    start = Cube(x=data["start"]["x"], y=data["start"]["y"], grounded=True)
    sim = simulate(world, params, [{"tick": t, "action": a} for r in data["route"]
                                   for t, a in ((r, "press"), (r + 1, "release"))],
                   max_ticks=max(data["trace"]["tick"]), start=start, start_tick=0)
    assert sim.death is None
    simulated = dict(zip(sim.trace["tick"], zip(sim.trace["x"], sim.trace["y"], strict=True),
                         strict=True))
    trace = data["trace"]
    compared = 0
    for tick, x, y in zip(trace["tick"], trace["x"], trace["y"], strict=True):
        s = simulated.get(tick - 1)
        if s is None:
            continue
        compared += 1
        assert abs(s[0] - x) < 0.01 and abs(s[1] - y) < 0.01, (tick, (x, y), s)
    assert compared > 1000
