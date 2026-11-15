"""The plan, beat and music tools in real turns against the fake editor (M4)."""

from __future__ import annotations

import json

from test_design_agent import ScriptProvider, call, text

from evals.fake_editor import FakeEditor
from gd_ai_assistant import catalog
from gd_ai_assistant.agent import History, ToolLoop
from gd_ai_assistant.design.plan import PlanStore


def _loop(editor, provider, store) -> ToolLoop:
    return ToolLoop(link=editor, provider=provider, catalog=catalog.load_catalog(),
                    history=History(token_budget=8000), max_tool_rounds=10,
                    turn_timeout_s=5, max_retries=0, retry_base_s=0.0,
                    plan_store=store, level_key="level-1", level_name="Test level")


def _results(provider: ScriptProvider) -> list[dict]:
    return [json.loads(m.text) for m in provider.seen_messages if m.role == "tool"]


class SystemSpy(ScriptProvider):
    """Also keeps the system prompt of every call."""

    def __init__(self, turns) -> None:
        super().__init__(turns)
        self.systems: list[str] = []

    async def complete(self, *, system, messages, tools):  # noqa: A002
        self.systems.append(system)
        return await super().complete(system=system, messages=messages, tools=tools)


async def test_a_plan_is_written_read_and_shown_in_the_prompt(tmp_path) -> None:
    store = PlanStore(tmp_path)
    ops = [{"op": "set_field", "path": "concept", "value": "Calm intro, hard drop."},
           {"op": "set_music", "bpm": 120, "offset_ms": 0, "source": "user"},
           {"op": "add_section", "section": {"id": "s1", "name": "Intro", "start_ms": 0,
                                             "end_ms": 4000, "difficulty": 2}}]
    provider = SystemSpy([call("update_level_plan", {"ops": ops}),
                          call("get_level_plan", {}, "c2"),
                          call("beat_grid", {"x_range": [0, 600]}, "c3"), text()])
    await _loop(FakeEditor(), provider, store).run(turn_id="t1", message_id="m1",
                                                  user_text="plan it")
    saved, read, grid = _results(provider)
    assert saved["ok"] and store.load("level-1")["sections"][0]["name"] == "Intro"
    section = read["plan"]["sections"][0]
    assert section["x_range"][0] == 0 and section["x_range"][1] > 1000
    assert grid["beats"][0]["strength"] == "bar"
    assert "## Level plan" not in provider.systems[0]   # no plan before the first write
    assert "Calm intro, hard drop." in provider.systems[-1]


async def test_beat_grid_without_a_bpm_says_to_ask(tmp_path) -> None:
    provider = ScriptProvider([call("beat_grid", {"x_range": [0, 600]}), text()])
    await _loop(FakeEditor(), provider, PlanStore(tmp_path)).run(
        turn_id="t1", message_id="m1", user_text="beats?")
    assert "ask the user" in _results(provider)[0]["error"]


async def test_a_bad_op_changes_nothing(tmp_path) -> None:
    store = PlanStore(tmp_path)
    provider = ScriptProvider([call("update_level_plan", {"ops": [
        {"op": "add_section", "section": {"id": "s1", "name": "A", "difficulty": 99}}]}),
        text()])
    await _loop(FakeEditor(), provider, store).run(turn_id="t1", message_id="m1",
                                                  user_text="go")
    assert "nothing was changed" in _results(provider)[0]["error"]
    assert store.load("level-1") is None


async def test_verified_sections_record_their_gameplay_and_go_stale(tmp_path) -> None:
    store = PlanStore(tmp_path)
    editor = FakeEditor()
    editor.add(8, 255, 15)
    mark = {"op": "add_section", "section": {
        "id": "s1", "name": "Spike", "x_range": [0, 600],
        "status": {"gameplay": "verified"}}}
    provider = ScriptProvider([call("update_level_plan", {"ops": [mark]}), text()])
    await _loop(editor, provider, store).run(turn_id="t1", message_id="m1", user_text="ok")
    assert store.load("level-1")["sections"][0]["verified"]["hash"]

    editor.add(8, 405, 15)                         # the user adds a spike by hand
    provider = ScriptProvider([call("get_level_plan", {}), text()])
    await _loop(editor, provider, store).run(turn_id="t2", message_id="m2", user_text="?")
    assert _results(provider)[0]["plan"]["progress"]["stale"] == ["s1"]


async def test_place_grid_reports_beat_offsets(tmp_path) -> None:
    store = PlanStore(tmp_path)
    setup = ScriptProvider([call("update_level_plan", {"ops": [
        {"op": "set_music", "bpm": 120, "offset_ms": 0}]}), text()])
    editor = FakeEditor()
    await _loop(editor, setup, store).run(turn_id="t1", message_id="m1", user_text="bpm")
    grid = "@grid col=8 row=0 w=6 h=3\n......\n..o...\n.....^"
    provider = ScriptProvider([call("place_grid", {
        "grid": grid, "preview": True, "verify": {"speed": "1x"},
        "snap_to_beats": {"subdivision": 2}}), text()])
    await _loop(editor, provider, store).run(turn_id="t2", message_id="m2", user_text="go")
    beats = _results(provider)[0]["beats"]
    assert beats["items"] and {"what", "x", "off_beat_ms", "on_beat"} <= set(beats["items"][0])
    assert any(item["what"] == "orb" for item in beats["items"])


async def test_level_music_through_the_agent() -> None:
    editor = FakeEditor()
    editor.music = {"song_type": 0, "audio_track": 3, "song_name": "Base After Base"}
    provider = ScriptProvider([call("get_level_music", {}), text()])
    await _loop(editor, provider, None).run(turn_id="t1", message_id="m1", user_text="song?")
    assert _results(provider)[0]["audio_track"] == 3
