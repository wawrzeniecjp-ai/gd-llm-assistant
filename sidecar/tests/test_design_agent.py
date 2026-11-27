"""The tool loop against the fake editor, and tool hiding for older mods (LD14, LD17)."""

from __future__ import annotations

from evals.fake_editor import FakeEditor
from gd_ai_assistant import catalog, tools
from gd_ai_assistant.agent import COMPLETED, History, ToolLoop
from gd_ai_assistant.providers.base import AssistantTurn, Provider, ToolCall, Usage


class ScriptProvider(Provider):
    """Plays a list of turns and records the tool list it was offered each time."""

    name = "script"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.offered: list[list[str]] = []
        self.seen_messages: list = []

    async def complete(self, *, system, messages, tools) -> AssistantTurn:  # noqa: A002
        self.offered.append([spec.name for spec in tools])
        self.seen_messages = list(messages)
        return self._turns.pop(0)


def call(name: str, arguments: dict, call_id: str = "c1") -> AssistantTurn:
    return AssistantTurn(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        stop_reason="tool_use",
        usage=Usage(1, 1),
    )


def text(value: str = "Done.") -> AssistantTurn:
    return AssistantTurn(text=value, stop_reason="end_turn", usage=Usage(1, 1))


def make_loop(link, provider: Provider) -> ToolLoop:
    return ToolLoop(
        link=link,
        provider=provider,
        catalog=catalog.load_catalog(),
        history=History(token_budget=8000),
        max_tool_rounds=10,
        turn_timeout_s=5,
        max_retries=0,
        retry_base_s=0.0,
    )


async def test_a_mutating_turn_against_the_fake_editor() -> None:
    editor = FakeEditor()
    objects = [{"id": 1, "x": 15, "y": 15}, {"id": 8, "x": 45, "y": 15}]
    provider = ScriptProvider([call("create_objects", {"objects": objects}), text()])
    result = await make_loop(editor, provider).run(turn_id="t1", message_id="m1", user_text="go")

    assert result.status == COMPLETED
    assert len(editor.objects) == 2
    types = [t for t, _ in editor.requests]
    begin, create = types.index("begin_transaction"), types.index("create_objects")
    assert begin < create < types.index("end_transaction")
    assert result.txn_id is not None


async def test_unsupported_request_hides_the_tool_for_the_rest_of_the_session() -> None:
    editor = FakeEditor(unsupported={"get_camera"})
    editor.unsupported_requests = set()  # what the server provides per connection
    provider = ScriptProvider([call("get_camera", {}), text()])

    await make_loop(editor, provider).run(turn_id="t1", message_id="m1", user_text="where?")

    assert "get_camera" in provider.offered[0]
    assert "get_camera" not in provider.offered[1]
    tool_message = next(m for m in provider.seen_messages if m.role == "tool")
    assert "UNSUPPORTED_REQUEST" in tool_message.text
    assert "newer mod" in tool_message.text

    # The next turn on the same connection never offers it at all.
    provider2 = ScriptProvider([text()])
    await make_loop(editor, provider2).run(turn_id="t2", message_id="m2", user_text="again")
    assert "get_camera" not in provider2.offered[0]


async def test_without_a_shared_set_the_hiding_lasts_one_turn() -> None:
    editor = FakeEditor(unsupported={"get_camera"})
    await make_loop(editor, ScriptProvider([call("get_camera", {}), text()])).run(
        turn_id="t1", message_id="m1", user_text="x"
    )
    provider = ScriptProvider([text()])
    await make_loop(editor, provider).run(turn_id="t2", message_id="m2", user_text="y")
    assert "get_camera" in provider.offered[0]


def test_tool_specs_filters_on_required_requests() -> None:
    every = {spec.name for spec in tools.tool_specs()}
    without = {spec.name for spec in tools.tool_specs({"create_objects"})}
    # place_grid and place_pattern build through create_objects, so they are hidden with it.
    assert every - without == {"create_objects", "place_grid", "place_pattern"}
    # Local tools with no requirements are never hidden.
    assert "search_object_catalog" in without


async def test_snapshot_is_dropped_after_a_mutation() -> None:
    editor = FakeEditor()
    editor.add(1, 15, 15)
    loop = make_loop(editor, ScriptProvider([
        call("create_objects", {"objects": [{"id": 1, "x": 45, "y": 15}]}),
        text(),
    ]))
    before = await loop._snapshot.get()
    assert len(before) == 1

    await loop.run(turn_id="t1", message_id="m1", user_text="add one")

    assert not loop._snapshot.cached
    assert len(await loop._snapshot.get()) == 2
