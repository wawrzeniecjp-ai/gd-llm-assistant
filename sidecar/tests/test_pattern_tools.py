"""`search_patterns`, `place_pattern` and `extract_pattern` (LEVEL-DESIGN-SPEC.md 10.4),
directly and through the tool loop against the fake editor."""

from __future__ import annotations

import json

import pytest

from evals.fake_editor import FakeEditor
from gd_ai_assistant import catalog
from gd_ai_assistant.agent import COMPLETED, History, ToolLoop
from gd_ai_assistant.design import pattern_tools as T
from gd_ai_assistant.design import patterns as P
from gd_ai_assistant.providers.base import AssistantTurn, Provider, ToolCall, Usage


class Script(Provider):
    name = "script"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.results: list[dict] = []

    async def complete(self, *, system, messages, tools) -> AssistantTurn:  # noqa: A002
        for message in messages[::-1]:
            if message.role == "tool":
                self.results.append(json.loads(message.text))
                break
        return self._turns.pop(0)


def call(name: str, arguments: dict) -> AssistantTurn:
    return AssistantTurn(tool_calls=[ToolCall(id=f"c-{name}", name=name, arguments=arguments)],
                         stop_reason="tool_use", usage=Usage(1, 1))


DONE = AssistantTurn(text="Done.", stop_reason="end_turn", usage=Usage(1, 1))


async def run(editor: FakeEditor, *calls: AssistantTurn) -> Script:
    provider = Script([*calls, DONE])
    loop = ToolLoop(link=editor, provider=provider, catalog=catalog.load_catalog(),
                    history=History(token_budget=8000), max_tool_rounds=10,
                    turn_timeout_s=30, max_retries=0, retry_base_s=0.0)
    result = await loop.run(turn_id="t1", message_id="m1", user_text="go")
    assert result.status == COMPLETED
    return provider


# -- direct -----------------------------------------------------------------------------------


def test_search_lists_previews_and_chaining_states() -> None:
    found = T.search_patterns({"query": "triple spike", "speed": "1x"})["patterns"]
    top = found[0]
    assert top["id"] == "cube.triple-spike.1x"
    assert top["grid"].startswith("@grid col=0 row=0")
    assert top["entry"]["row"] == 0 and top["exit"]["mode"] == "cube"
    assert T.search_patterns({"query": "nothing-like-this"})["patterns"] == []


def test_prepare_place_fills_in_verify_from_the_entry() -> None:
    args = T.prepare_place({"id": "ship.slalom.1x", "col": 40, "row": 0})
    assert args["mode"] == "add" and args["grid"].startswith("@grid col=40 row=0")
    assert args["verify"]["gamemode"] == "ship" and args["verify"]["entry_row"] == 3
    assert T.prepare_place({"id": "nope", "col": 0})["error"].startswith("no pattern")


def test_a_variant_that_does_not_play_places_nothing() -> None:
    refused = T.prepare_place({"id": "cube.triple-spike.1x", "col": 10,
                               "variant": "rescale_speed:0.5x"})
    assert "does not play at 0.5x" in refused["error"] and "nothing was placed" in refused[
        "error"]
    flipped = T.prepare_place({"id": "cube.triple-spike.1x", "col": 10, "row": 4,
                               "variant": "flip_gravity"})
    assert flipped["verify"]["gravity"] == -1 and flipped["_pattern"]["id"].endswith(
        ".flip.1x")


# -- through the tool loop ------------------------------------------------------------------


async def test_place_pattern_builds_and_verifies_like_place_grid() -> None:
    editor = FakeEditor()
    provider = await run(editor, call("place_pattern", {"id": "cube.triple-spike.1x",
                                                        "col": 20}))
    result = provider.results[-1]
    assert result["created"] == 3 and len(editor.objects) == 3
    assert result["verification"]["result"] == "possible"
    assert result["pattern"]["id"] == "cube.triple-spike.1x" and result["pattern"]["col"] == 20


async def test_place_pattern_refuses_occupied_cells() -> None:
    editor = FakeEditor()
    first = {"id": "cube.triple-spike.1x", "col": 20}
    provider = await run(editor, call("place_pattern", first), call("place_pattern", first))
    assert "already hold gameplay" in provider.results[-1]["error"]
    assert len(editor.objects) == 3


async def test_extract_pattern_saves_to_the_user_library(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    editor = FakeEditor()
    provider = await run(
        editor,
        call("place_pattern", {"id": "cube.spike-on-block.1x", "col": 30}),
        call("extract_pattern", {"x_range": [900, 1380], "name": "My spike block",
                                 "tags": ["mine"]}),
    )
    saved = provider.results[-1]
    assert saved["saved"] is True and saved["pattern"]["id"] == "user.my-spike-block.1x"
    library = P.load_library()
    mine = next(p for p in library if p.id == "user.my-spike-block.1x")
    assert mine.source["kind"] == "user" and mine.verified["result"] == "possible"
    assert "mine" in mine.tags


@pytest.mark.parametrize("args, message", [
    ({"x_range": [0, 300]}, "name"),
    ({"name": "x"}, "x_range"),
])
def test_extract_needs_a_name_and_a_range(args, message) -> None:
    from gd_ai_assistant.design.coords import Grid
    from gd_ai_assistant.design.level_model import LevelModel
    from gd_ai_assistant.design.roles import load_role_table

    out = T.extract_pattern(LevelModel([], revision=0), Grid(0.0), load_role_table(), args)
    assert message in out["error"]
