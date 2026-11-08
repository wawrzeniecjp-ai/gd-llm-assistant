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


@pytest.mark.parametrize("section", SECTIONS, ids=lambda s: f"slot{s.slot}")
def test_draft_classified_as_intended(level, section) -> None:
    world, params = level
    assert verify(world, params, verify_args(section.slot))["result"] == section.expected
