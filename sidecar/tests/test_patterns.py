"""The pattern library (LEVEL-DESIGN-SPEC.md 10): format, variants, and the seed library.

Re-verification (10.3) has two layers. Every run checks that each seed pattern was verified
with the current constants file, so a constants change fails here until the library is
rebuilt with `scripts/build_seed_patterns.py`, which re-verifies every pattern. Every run
also re-verifies one pattern per mode. Re-verifying all of them takes minutes; set
`GD_FULL_PATTERNS=1` to do it here.
"""

from __future__ import annotations

import os
from collections import Counter

import pytest

from gd_ai_assistant.design import patterns as P
from gd_ai_assistant.design.constants import SPEEDS

SEEDS = P.load_seed()


def _pattern(rows: list[str], **overrides) -> P.Pattern:
    raw = {"schema": 1, "id": "cube.test.1x", "name": "Test", "mode": "cube", "speed": "1x",
           "difficulty": 1, "grid": f"@grid col=0 row=0 w={len(rows[0])} h={len(rows)}\n"
           + "\n".join(rows), "exit": {"row": 0, "grounded": True}, **overrides}
    return P.Pattern.from_json(raw)


TRIPLE = ["." * 12] * 3 + [".....^^^...."]


def test_a_malformed_pattern_lists_its_problems() -> None:
    with pytest.raises(P.PatternError) as raised:
        P.Pattern.from_json({"schema": 2, "id": "Bad Id", "mode": "rocket", "speed": "9x",
                             "difficulty": 11, "grid": "@grid col=3 row=0 w=2 h=1\n.."})
    message = str(raised.value)
    for part in ("schema", "id", "mode", "speed", "difficulty", "relative"):
        assert part in message


def test_a_pattern_round_trips_through_json() -> None:
    p = _pattern(TRIPLE, tags=["cube", "spike"])
    assert P.Pattern.from_json(p.to_json()) == p


def test_verify_plays_the_pattern_alone_from_its_entry() -> None:
    record = P.verify_pattern(_pattern(TRIPLE))
    assert record["result"] == "possible" and record["inputs"] == 1
    assert record["exit"]["grounded"] and record["constants_version"] == P.constants_version()


def test_verify_refuses_a_wrong_exit() -> None:
    record = P.verify_pattern(_pattern(TRIPLE, exit={"row": 2, "grounded": True}))
    assert record["result"] == "exit_mismatch" and "row 0" in record["reason"]


def test_rescale_keeps_obstacles_and_re_spaces_them() -> None:
    """A triple spike stays a triple spike at 4x; at 0.5x it cannot be jumped, so it is
    refused rather than placed."""
    fast = P.rescaled(_pattern(TRIPLE), "4x")
    assert fast.speed == "4x" and fast.id == "cube.test.4x"
    assert "^^^" in fast.grid and fast.width > 12
    with pytest.raises(P.PatternError, match="does not play at 0.5x"):
        P.rescaled(_pattern(TRIPLE), "0.5x")


def test_flip_mirrors_the_pattern_and_adds_its_ceiling() -> None:
    flipped = P.flipped(_pattern(TRIPLE))
    assert flipped.gravity == -1 and flipped.id == "cube.test.flip.1x"
    rows = flipped.grid.split("\n")[1:]
    assert rows[0] == "#" * 12 and "vvv" in rows[1]
    assert flipped.entry["row"] == 3 and flipped.verified["result"] == "possible"
    assert flipped.verified["min_window_ms"] == P.verify_pattern(_pattern(TRIPLE))[
        "min_window_ms"]


def test_flight_patterns_and_pads_cannot_be_flipped() -> None:
    with pytest.raises(P.PatternError, match="band"):
        P.flip_gravity(_pattern(["." * 6, "..^^.."], mode="ship"))
    with pytest.raises(P.PatternError, match="upside-down"):
        P.flip_gravity(_pattern(["." * 6, ".._..."]))


def test_search_ranks_and_filters() -> None:
    found = P.search(SEEDS, query="spike", mode="cube", speed="1x", limit=5)
    assert found and all(p.mode == "cube" and p.speed == "1x" for p in found)
    assert all("spike" in p.tags or "spike" in p.id for p in found)
    hard = P.search(SEEDS, mode="cube", speed="1x", difficulty=(5, 10), limit=50)
    assert hard and all(p.difficulty >= 5 for p in hard)
    ship_in = P.search(SEEDS, query="ship", tags=["transition"], limit=50)
    assert [p.id for p in ship_in][:1] == ["transition.cube-ship-cube.1x"]


# -- the seed library ----------------------------------------------------------------------


def test_the_seed_library_meets_its_target() -> None:
    """10.5: at least 60 at 1x: 25 cube, 10 ship, 8 ball, 6 ufo, 6 wave, 5 transitions."""
    counts = Counter("transition" if p.id.startswith("transition.") else p.mode
                     for p in SEEDS if p.speed == "1x")
    assert sum(counts.values()) >= 60
    for kind, least in {"cube": 25, "ship": 10, "ball": 8, "ufo": 6, "wave": 6,
                        "transition": 5}.items():
        assert counts[kind] >= least, (kind, counts[kind])


def test_every_seed_is_verified_with_the_current_constants() -> None:
    """A constants change fails here until `scripts/build_seed_patterns.py` re-verifies the
    library (10.3)."""
    current = P.constants_version()
    stale = [p.id for p in SEEDS
             if not p.verified or p.verified.get("constants_version") != current]
    assert not stale, f"re-run scripts/build_seed_patterns.py: {stale[:5]}"
    assert all(p.verified["result"] == "possible" for p in SEEDS)


def test_every_other_speed_comes_from_a_1x_pattern() -> None:
    ids = {p.id for p in SEEDS}
    for p in SEEDS:
        assert p.speed in SPEEDS
        if p.speed != "1x":
            assert P.base_id(p) + ".1x" in ids, p.id


def test_seeds_never_ask_for_a_frame_perfect_input() -> None:
    windows = [(p.id, p.verified.get("min_window_ms")) for p in SEEDS]
    assert all(w is None or w > 4.2 for _, w in windows), [x for x in windows if x[1]
                                                            and x[1] <= 4.2]


def _sample() -> list[P.Pattern]:
    seen: dict[str, P.Pattern] = {}
    for p in SEEDS:
        if p.speed == "1x":
            seen.setdefault(p.mode, p)
    return list(seen.values())


@pytest.mark.parametrize("pattern", _sample(), ids=lambda p: p.id)
def test_a_seed_of_each_mode_still_verifies(pattern: P.Pattern) -> None:
    record = P.verify_pattern(pattern)
    assert record["result"] == "possible", record
    assert record["min_window_ms"] == pattern.verified["min_window_ms"]


@pytest.mark.skipif(not os.environ.get("GD_FULL_PATTERNS"),
                    reason="re-verifies every seed (minutes); set GD_FULL_PATTERNS=1")
@pytest.mark.parametrize("pattern", SEEDS, ids=lambda p: p.id)
def test_every_seed_still_verifies(pattern: P.Pattern) -> None:
    assert P.verify_pattern(pattern)["result"] == "possible"
