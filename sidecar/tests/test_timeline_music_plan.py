"""Timeline, beat grid and level plan (LEVEL-DESIGN-SPEC.md 8, 9)."""

from __future__ import annotations

import json
import math

import pytest

from evals.fake_editor import FakeEditor
from gd_ai_assistant.design import music, plan
from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import CubeParams, simulate
from gd_ai_assistant.design.sim.world import world_from_objects
from gd_ai_assistant.design.timeline import Timeline

PARAMS = CubeParams.from_constants(load_constants())
ROLES = load_role_table()
#: Speed portal ids by speed, from the role table.
PORTAL = {e.variant: e.id for e in ROLES.entries.values() if e.role.value == "portal_speed"}
#: Five portals along a flat floor: every speed, each change at least 300 units on.
COURSE = [(600, "2x"), (1500, "0.5x"), (2100, "3x"), (3300, "4x"), (4800, "1x")]


def _course_model():
    editor = FakeEditor()
    for x, speed in COURSE:
        editor.add(PORTAL[speed], x, 45)
    return editor, editor.level_model()


def test_timeline_matches_the_simulator_within_a_tick_at_five_portals() -> None:
    """M4's done-when, against the simulator (which matches the game's replays)."""
    editor, model = _course_model()
    timeline = Timeline.from_model(model, PARAMS)
    assert [s.speed for s in timeline.segments] == ["1x", "2x", "0.5x", "3x", "4x", "1x"]
    world = world_from_objects([{"id": o.type_id, "x": o.props["x"], "y": o.props["y"]}
                                for o in editor.objects.values()], ROLES)
    run = simulate(world, PARAMS, [], max_ticks=20_000, until_x=6000)
    first_tick: dict[int, int] = {}
    for x_probe in range(100, 6000, 97):
        first_tick[x_probe] = next(t for t, x in zip(run.trace["tick"], run.trace["x"],
                                                      strict=True) if x >= x_probe)
    for x_probe, tick in first_tick.items():
        # Sample 1 is the level start's command phase, with no move (sim/cube.py).
        # The first whole tick at or past it, as the simulator samples it.
        predicted = math.ceil(timeline.ms_at(x_probe) * 0.24 + 1 - 1e-9)
        assert abs(tick - predicted) <= 1, (x_probe, tick, predicted)


def test_x_and_ms_are_inverse_and_follow_the_song_offset() -> None:
    _, model = _course_model()
    timeline = Timeline.from_model(model, PARAMS, start_speed="2x", song_offset_s=1.5)
    for x in (0.0, 450.0, 1499.0, 2500.0, 5000.0):
        assert timeline.x_at(timeline.ms_at(x)) == pytest.approx(x, abs=1e-6)
        assert timeline.song_ms_at(x) == pytest.approx(timeline.ms_at(x) + 1500.0)
    assert timeline.speed_at(10) == "2x"


def test_beat_grid_marks_bars_beats_and_subdivisions() -> None:
    timeline = Timeline.build([], PARAMS)                  # 1x all the way
    tune = {"bpm": 120.0, "offset_ms": 0.0, "beats_per_bar": 4}
    grid = music.beat_grid(tune, timeline, (0, timeline.x_at(2100)), subdivision=2)
    beats = grid["beats"]
    assert [b["ms"] for b in beats] == [0, 250, 500, 750, 1000, 1250, 1500, 1750, 2000]
    assert [b["strength"] for b in beats[:4]] == ["bar", "sub", "beat", "sub"]
    assert beats[8]["strength"] == "bar" and beats[8]["bar"] == 2
    ups = PARAMS.dx["1x"] * 240
    assert grid["units_per_beat_at_start"] == pytest.approx(ups * 0.5, abs=0.1)


def test_tempo_changes_restart_the_spacing_from_their_time() -> None:
    tune = {"bpm": 60.0, "offset_ms": 0.0, "tempo_changes": [{"at_ms": 2000, "bpm": 120.0}]}
    times = [ms for ms, _ in music.beat_times(tune, 0, 3600)]
    assert times == [0, 1000, 2000, 2500, 3000, 3500]


def test_alignment_counts_presses_near_a_subdivision() -> None:
    timeline = Timeline.build([], PARAMS)
    tune = {"bpm": 120.0, "offset_ms": 0.0}
    on = timeline.x_at(500 + 10)          # 10 ms late
    off = timeline.x_at(625)              # halfway between half-beats
    result = music.alignment(tune, timeline, [on, off], subdivision=2)
    assert result["on_beat"] == 1 and result["fraction"] == 0.5


def test_no_bpm_is_an_error_that_says_to_ask() -> None:
    with pytest.raises(music.MusicError, match="ask the user"):
        music.beat_times({"bpm": None}, 0, 1000)


# -- the plan ------------------------------------------------------------------------


def test_ops_build_a_plan_and_one_bad_op_rejects_the_batch() -> None:
    base = plan.new_plan("key", "My level")
    built = plan.apply_ops(base, [
        {"op": "set_field", "path": "concept", "value": "A calm start, a hard drop."},
        {"op": "set_music", "bpm": 128, "offset_ms": 230, "source": "user"},
        {"op": "add_section", "section": {"id": "s1", "name": "Intro", "start_ms": 0,
                                          "end_ms": 8000, "speed": "1x", "difficulty": 3}},
        {"op": "add_decision", "text": "No dual sections."},
    ])
    assert built["music"]["bpm"] == 128 and built["sections"][0]["status"]["gameplay"] == "todo"
    with pytest.raises(plan.PlanError, match="nothing was changed"):
        plan.apply_ops(built, [
            {"op": "update_section", "id": "s1", "fields": {"difficulty": 4}},
            {"op": "update_section", "id": "s1", "fields": {"difficulty": 11}},
        ])
    with pytest.raises(plan.PlanError, match="end_ms must be after"):
        plan.apply_ops(built, [{"op": "update_section", "id": "s1",
                                "fields": {"end_ms": 0}}])
    assert base["sections"] == []                          # never changed in place


def test_store_writes_atomically_and_keeps_a_backup(tmp_path) -> None:
    store = plan.PlanStore(tmp_path)
    first = plan.new_plan("level-a", "A")
    store.save(first)
    second = plan.apply_ops(first, [{"op": "set_field", "path": "concept", "value": "x"}])
    store.save(second)
    assert store.load("level-a")["concept"] == "x"
    backup = store.path("level-a").with_suffix(".json.bak")
    assert json.loads(backup.read_text(encoding="utf-8"))["concept"] == ""
    assert store.load("level-b") is None


def test_sections_follow_the_music_and_go_stale_when_their_gameplay_changes() -> None:
    editor, model = _course_model()
    timeline = Timeline.from_model(model, PARAMS)
    built = plan.apply_ops(plan.new_plan("k", "L"), [
        {"op": "add_section", "section": {"id": "s1", "name": "Fast", "start_ms": 1000,
                                          "end_ms": 2000}}])
    span = plan.section_x_range(built["sections"][0], timeline)
    assert span == (round(timeline.x_at(1000), 1), round(timeline.x_at(2000), 1))
    built["sections"][0]["status"]["gameplay"] = "verified"
    built["sections"][0]["verified"] = {"hash": plan.gameplay_hash(model, span)}
    assert not plan.with_derived(built, timeline, model, "L")["progress"]["stale"]
    editor.add(8, (span[0] + span[1]) / 2, 15)              # a spike inside the section
    shown = plan.with_derived(built, timeline, editor.level_model(), "Other")
    assert shown["progress"]["stale"] == ["s1"] and "warning" in shown


def test_the_prompt_block_is_short() -> None:
    big = plan.new_plan("k", "L")
    big["sections"] = [{"id": f"s{i}", "name": "x" * 60, "start_ms": i * 1000,
                        "end_ms": i * 1000 + 999} for i in range(60)]
    block = plan.render_block(big)
    assert block.startswith("## Level plan") and len(block) <= 2400
