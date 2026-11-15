"""The tool loop, tested against a fake editor and a scripted provider (SPEC.md 13).

These are the tests the Phase 4 exit criterion names. They drive complete turns --
tool dispatch, transaction lifecycle, caps, cancellation, provider failure --
without a WebSocket, a network or a provider account, because the loop talks to
both sides through narrow interfaces.

The transaction tests are the ones worth reading. An abandoned transaction is not
a cosmetic problem: it stays open in the mod, and every later turn's
`begin_transaction` then fails with `TRANSACTION_ACTIVE`, so one leak breaks the
session. A read-only turn opening one is equally bad in the other direction: it
leaves an empty entry in the user's undo history, so Ctrl+Z appears to do nothing.
"""

from __future__ import annotations

import asyncio

import pytest
from jsonschema import Draft202012Validator

from gd_ai_assistant import catalog, tools
from gd_ai_assistant.agent import (
    COMPLETED,
    FAILED,
    INCOMPLETE,
    REVERTED_NOTE,
    History,
    ToolLoop,
)
from gd_ai_assistant.providers import FailingProvider, ScriptedProvider
from gd_ai_assistant.providers.base import (
    AssistantTurn,
    Message,
    Provider,
    ProviderError,
    ToolCall,
    Usage,
)
from gd_ai_assistant.server import Response


class FakeLink:
    """A stand-in for the mod, enforcing the protocol rules the loop depends on."""

    def __init__(self, results: dict[str, dict] | None = None) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.events: list[tuple[str, dict]] = []
        self._results = results or {}
        #: The open transaction id, or None. Mirrors the mod's own state so that
        #: a leaked or doubled transaction is visible in the assertions.
        self.open_txn: str | None = None
        self._txn_counter = 0

    async def request(self, type_name: str, payload: dict | None = None) -> Response:
        payload = payload or {}
        self.requests.append((type_name, payload))

        if type_name == "begin_transaction":
            if self.open_txn is not None:
                # What the real mod returns, and exactly the failure a leaked
                # transaction produces.
                return Response(
                    ok=False,
                    error={
                        "code": "TRANSACTION_ACTIVE",
                        "message": "a transaction is already open",
                    },
                )
            self._txn_counter += 1
            self.open_txn = f"x-{self._txn_counter}"
            return Response(ok=True, result={"txn_id": self.open_txn})

        if type_name == "end_transaction":
            if payload.get("txn_id") != self.open_txn:
                return Response(
                    ok=False,
                    error={
                        "code": "TRANSACTION_NOT_REVERTIBLE",
                        "message": "no such open transaction",
                    },
                )
            self.open_txn = None
            return Response(
                ok=True,
                result={"txn_id": payload["txn_id"], "summary": {"created": 2}},
            )

        if type_name == "list_object_catalog":
            # What the mod returns from `ObjectToolbox`: real ids and the sprite each
            # one draws. Small on purpose -- the tests care about the plumbing, not
            # about GD's table.
            return Response(
                ok=True,
                result={
                    "entries": [
                        {"id": 1, "frame": "square_01_001.png", "grid_size": 30.0},
                        {"id": 8, "frame": "spike_01_001.png", "grid_size": 30.0},
                        {"id": 9, "frame": "spike_02_001.png", "grid_size": 15.0},
                    ],
                    "total": 3,
                    "next_offset": None,
                },
            )

        return Response(ok=True, result=self._results.get(type_name, {}))

    async def send_event(self, type_name: str, payload: dict) -> None:
        self.events.append((type_name, payload))

    def types(self) -> list[str]:
        return [name for name, _ in self.requests]


class BlockingProvider(Provider):
    """Returns one tool call, then blocks until cancelled.

    The only reliable way to cancel a loop mid-flight is for something in it to be
    awaiting, and this gives exactly one blocking point at a known position: after
    the transaction is open, so the cancellation cleanup has something to close.
    """

    name = "blocking"

    def __init__(self, call: ToolCall) -> None:
        self._call = call
        self._first = True

    async def complete(self, *, system, messages, tools) -> AssistantTurn:  # noqa: A002
        if self._first:
            self._first = False
            return AssistantTurn(tool_calls=[self._call], stop_reason="tool_use", usage=Usage(1, 1))
        await asyncio.Event().wait()  # never set; only cancellation ends this
        raise AssertionError("unreachable")


class RepeatingProvider(Provider):
    """Always asks for another tool call, to exercise the round cap."""

    name = "repeating"

    def __init__(self, call: ToolCall) -> None:
        self._call = call
        self.rounds = 0

    async def complete(self, *, system, messages, tools) -> AssistantTurn:  # noqa: A002
        self.rounds += 1
        return AssistantTurn(
            tool_calls=[
                ToolCall(
                    id=f"c-{self.rounds}", name=self._call.name, arguments=self._call.arguments
                )
            ],
            stop_reason="tool_use",
            usage=Usage(1, 1),
        )


class SleepingProvider(Provider):
    """Sleeps instead of answering, to exercise the wall-clock timeout."""

    name = "sleeping"

    async def complete(self, *, system, messages, tools) -> AssistantTurn:  # noqa: A002
        await asyncio.sleep(10)
        raise AssertionError("unreachable")


class FailsAfterFirstCall(Provider):
    """Applies one mutation, then fails: for the partial-failure message."""

    name = "fails-after-first"

    def __init__(self, call: ToolCall) -> None:
        self._call = call
        self._first = True

    async def complete(self, *, system, messages, tools) -> AssistantTurn:  # noqa: A002
        if self._first:
            self._first = False
            return AssistantTurn(tool_calls=[self._call], stop_reason="tool_use", usage=Usage(1, 1))
        raise ProviderError("connection reset")


def make_loop(link: FakeLink, provider: Provider, **overrides) -> ToolLoop:
    defaults = {
        "max_tool_rounds": 10,
        "max_objects_per_turn": 5000,
        "turn_timeout_s": 5,
        # Failures in these tests are deliberate, so the backoff must not add
        # seconds of waiting to every run.
        "max_retries": 0,
        "retry_base_s": 0.0,
    }
    return ToolLoop(
        link=link,
        provider=provider,
        catalog=catalog.load_catalog(),
        history=History(token_budget=8000),
        **(defaults | overrides),
    )


def create_turn(count: int = 2) -> AssistantTurn:
    objects = [{"id": 8, "x": 300.0 + index * 30, "y": 105.0} for index in range(count)]
    return AssistantTurn(
        text="",
        tool_calls=[ToolCall(id="c-create", name="create_objects", arguments={"objects": objects})],
        stop_reason="tool_use",
        usage=Usage(100, 20),
    )


def text_turn(text: str = "Done.") -> AssistantTurn:
    return AssistantTurn(text=text, stop_reason="end_turn", usage=Usage(50, 10))


# -- tool dispatch -------------------------------------------------------


async def test_read_only_turn_opens_no_transaction() -> None:
    link = FakeLink()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="get_editor_state", arguments={})],
                stop_reason="tool_use",
                usage=Usage(10, 5),
            ),
            text_turn(),
        ]
    )
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="look")

    assert result.status == COMPLETED
    assert link.types() == ["get_editor_state"]
    assert link.open_txn is None
    # The panel offers Revert from `txn_id`; a read-only turn must not offer it.
    assert result.txn_id is None


async def test_mutating_turn_opens_and_closes_exactly_one_transaction() -> None:
    link = FakeLink({"create_objects": {"created": [1, 2], "count": 2}})
    provider = ScriptedProvider([create_turn(2), text_turn("Placed two spikes.")])
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    assert result.status == COMPLETED
    assert link.types().count("begin_transaction") == 1
    assert link.types().count("end_transaction") == 1
    assert link.open_txn is None
    assert result.txn_id == "x-1"


async def test_the_transaction_opens_before_the_first_mutation() -> None:
    link = FakeLink()
    provider = ScriptedProvider([create_turn(1), text_turn()])
    await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    order = link.types()
    assert order.index("begin_transaction") < order.index("create_objects")
    assert order.index("create_objects") < order.index("end_transaction")


async def test_tool_arguments_are_the_protocol_payload() -> None:
    """No translation layer, so a field name cannot be wrong in two places."""
    link = FakeLink()
    provider = ScriptedProvider([create_turn(1), text_turn()])
    await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    payload = next(payload for name, payload in link.requests if name == "create_objects")
    assert payload == {"objects": [{"id": 8, "x": 300.0, "y": 105.0}]}


async def test_the_catalog_tool_is_answered_without_touching_the_mod() -> None:
    link = FakeLink()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="c1", name="search_object_catalog", arguments={"query": "spike"})
                ],
                stop_reason="tool_use",
                usage=Usage(1, 1),
            ),
            text_turn(),
        ]
    )
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="spike?")

    assert result.status == COMPLETED
    # The search itself is local. The one request that *is* made is the object table:
    # searching a two-entry seed is what made "add a thin spike" impossible, so the
    # loop reads the game's table before the model is asked anything.
    assert link.types() == ["list_object_catalog"]


async def test_a_thin_spike_is_found_by_size_when_no_name_matches() -> None:
    """The reason the game's table is fetched at all.

    "Thin" is a size, not a name, so no entry matches it literally. The search must
    still return the spikes, say which word matched nothing, and carry `grid_size` so
    the model can choose the narrow one instead of inventing an id.
    """
    link = FakeLink()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="search_object_catalog",
                        arguments={"query": "thin spike"},
                    )
                ],
                stop_reason="tool_use",
                usage=Usage(1, 1),
            ),
            text_turn(),
        ]
    )
    loop = make_loop(link, provider)
    await loop.run(turn_id="t-1", message_id="m1", user_text="add a thin spike")

    result = catalog.search_object_catalog(loop._catalog, {"query": "thin spike"})
    assert [match["id"] for match in result["matches"]] == [8, 9]
    assert result["unmatched_terms"] == ["thin"]
    assert result["source"] == "game"
    # The distinguishing fact, and the whole point of asking the game rather than a
    # hand-written table.
    assert [match["grid_size"] for match in result["matches"]] == [30.0, 15.0]
    assert "grid_size" in result["note"]


async def test_an_unknown_tool_is_reported_to_the_model_not_raised() -> None:
    """A hallucinated tool name is recoverable; killing the turn is not."""
    link = FakeLink()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="delete_everything", arguments={})],
                stop_reason="tool_use",
                usage=Usage(1, 1),
            ),
            text_turn("Sorry."),
        ]
    )
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    assert result.status == COMPLETED
    assert link.requests == []


async def test_a_failed_tool_call_is_reported_to_the_model() -> None:
    """A tool error becomes a result the model can act on, not an exception."""

    class FailingLink(FakeLink):
        async def request(self, type_name, payload=None):
            self.requests.append((type_name, payload or {}))
            return Response(
                ok=False,
                error={"code": "OBJECT_NOT_FOUND", "message": "none of those ids exist"},
            )

    link = FailingLink()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="modify_objects",
                        arguments={"targets": [1], "set": {"rotation": 90}},
                    )
                ],
                stop_reason="tool_use",
                usage=Usage(1, 1),
            ),
            text_turn("I could not find that object."),
        ]
    )
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    # Completed, not failed: the model was told and chose to answer. The turn did
    # open a transaction before the failed call, and that is closed correctly.
    assert result.status == COMPLETED
    assert link.open_txn is None
    assert result.text == "I could not find that object."


# -- limits --------------------------------------------------------------


async def test_the_object_cap_refuses_the_call_and_ends_the_turn() -> None:
    link = FakeLink()
    provider = ScriptedProvider([create_turn(10), text_turn("unreachable")])
    result = await make_loop(link, provider, max_objects_per_turn=5).run(
        turn_id="t-1", message_id="m1", user_text="go"
    )

    assert result.status == INCOMPLETE
    # Refused before mutating: the cap exists to bound the work, so checking it
    # after the call would exceed it on the very turn meant to be stopped.
    assert "create_objects" not in link.types()
    assert link.open_txn is None


async def test_the_object_cap_counts_across_calls_within_a_turn() -> None:
    link = FakeLink()
    provider = ScriptedProvider([create_turn(3), create_turn(3), text_turn()])
    result = await make_loop(link, provider, max_objects_per_turn=5).run(
        turn_id="t-1", message_id="m1", user_text="go"
    )

    assert result.status == INCOMPLETE
    assert link.types().count("create_objects") == 1


async def test_the_round_cap_ends_the_turn_incomplete() -> None:
    link = FakeLink()
    provider = RepeatingProvider(ToolCall(id="x", name="get_editor_state", arguments={}))
    result = await make_loop(link, provider, max_tool_rounds=3).run(
        turn_id="t-1", message_id="m1", user_text="go"
    )

    assert result.status == INCOMPLETE
    assert provider.rounds == 3
    assert result.text  # it must say something, not return an empty message


async def test_running_out_of_output_tokens_says_so() -> None:
    """2026-11-14: a thinking model spent its 4096 tokens reasoning and returned nothing;
    the panel said "Done." as if the turn had finished."""
    link = FakeLink({"create_objects": {"created": [1, 2], "count": 2}})
    cut = AssistantTurn(text="", stop_reason="max_tokens", usage=Usage(100, 4096))
    provider = ScriptedProvider([create_turn(2), cut])
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    assert result.status == INCOMPLETE
    assert "max_tokens" in result.text and "Edits made before that are kept" in result.text


async def test_the_wall_clock_timeout_ends_the_turn_incomplete() -> None:
    link = FakeLink()
    result = await make_loop(link, SleepingProvider(), turn_timeout_s=0.05).run(
        turn_id="t-1", message_id="m1", user_text="go"
    )

    assert result.status == INCOMPLETE
    assert result.text


# -- cancellation --------------------------------------------------------


async def test_cancellation_closes_the_open_transaction() -> None:
    """An abandoned transaction blocks every later turn, so this is load-bearing."""
    link = FakeLink()
    provider = BlockingProvider(
        ToolCall(id="c1", name="create_objects", arguments={"objects": [{"id": 8, "x": 0, "y": 0}]})
    )
    loop = make_loop(link, provider)
    task = asyncio.create_task(loop.run(turn_id="t-1", message_id="m1", user_text="go"))

    # Let the loop get as far as opening the transaction and blocking.
    for _ in range(50):
        if link.open_txn is not None:
            break
        await asyncio.sleep(0.01)
    assert link.open_txn is not None, "the loop never reached its mutating call"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cleanup runs shielded inside the cancellation, so the transaction is
    # closed even though the task itself ended.
    assert link.open_txn is None
    assert "end_transaction" in link.types()


# -- provider failure ----------------------------------------------------


async def test_a_provider_failure_reports_failed() -> None:
    link = FakeLink()
    result = await make_loop(link, FailingProvider("model exploded")).run(
        turn_id="t-1", message_id="m1", user_text="go"
    )

    assert result.status == FAILED
    assert "model exploded" in result.text


async def test_a_failure_after_an_edit_says_the_level_changed() -> None:
    """The user cannot tell from the panel whether the edit landed, so say so."""
    link = FakeLink()
    provider = FailsAfterFirstCall(
        ToolCall(
            id="c1",
            name="create_objects",
            arguments={"objects": [{"id": 8, "x": 0, "y": 0}]},
        )
    )
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    assert result.status == FAILED
    assert "review the level" in result.text.lower()
    # And the transaction opened for that edit is closed, not leaked.
    assert link.open_txn is None


async def test_a_retryable_failure_is_retried() -> None:
    link = FakeLink()
    provider = FailingProvider("rate limited", retryable=True)
    result = await make_loop(link, provider, max_retries=2).run(
        turn_id="t-1", message_id="m1", user_text="go"
    )

    assert result.status == FAILED
    # One initial attempt plus the two retries.
    assert provider.calls == 3


async def test_a_non_retryable_failure_is_not_retried() -> None:
    link = FakeLink()
    provider = FailingProvider("bad request", retryable=False)
    result = await make_loop(link, provider, max_retries=3).run(
        turn_id="t-1", message_id="m1", user_text="go"
    )

    assert result.status == FAILED
    assert provider.calls == 1


# -- reporting -----------------------------------------------------------


async def test_each_mutation_produces_one_action() -> None:
    link = FakeLink({"create_objects": {"created": [1, 2], "count": 2}})
    provider = ScriptedProvider([create_turn(2), text_turn()])
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    assert len(result.actions) == 1
    action = result.actions[0]
    # `kind` is a tool name: the schema's enum pins it (SPEC.md 7.4).
    assert action.kind == "create_objects"
    # From the result, not the request: a create where the mod rejected one id
    # must report what happened.
    assert action.count == 2


async def test_action_counts_come_from_the_result() -> None:
    link = FakeLink({"delete_objects": {"deleted_count": 1, "missing": [7, 8]}})
    provider = ScriptedProvider(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="c1", name="delete_objects", arguments={"targets": [1, 7, 8]})
                ],
                stop_reason="tool_use",
                usage=Usage(1, 1),
            ),
            text_turn(),
        ]
    )
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    assert result.actions[0].count == 1


async def test_usage_accumulates_across_rounds() -> None:
    link = FakeLink()
    provider = ScriptedProvider([create_turn(1), text_turn()])
    result = await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="go")

    assert result.usage.tool_rounds == 2
    assert result.usage.input_tokens == 150  # 100 + 50
    assert result.usage.output_tokens == 30  # 20 + 10


async def test_the_token_budget_ends_the_turn_incomplete() -> None:
    link = FakeLink()
    provider = ScriptedProvider([create_turn(1), text_turn()])
    result = await make_loop(link, provider, turn_token_budget=10).run(
        turn_id="t-1", message_id="m1", user_text="go"
    )

    assert result.status == INCOMPLETE


async def test_interim_text_is_sent_and_then_replaced_by_the_final() -> None:
    """One message id, so the panel shows one bubble filling in (SPEC.md 7.4)."""
    link = FakeLink()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                text="Looking at the level.",
                tool_calls=[ToolCall(id="c1", name="get_editor_state", arguments={})],
                stop_reason="tool_use",
                usage=Usage(1, 1),
            ),
            text_turn("There are 12 objects."),
        ]
    )
    await make_loop(link, provider).run(turn_id="t-1", message_id="m1", user_text="how many?")

    texts = [payload["text"] for name, payload in link.events if name == "assistant_message"]
    ids = {payload["message_id"] for name, payload in link.events if name == "assistant_message"}
    assert texts == ["Looking at the level.", "There are 12 objects."]
    assert ids == {"m1"}


# -- history -------------------------------------------------------------


async def test_history_keeps_only_completed_turns() -> None:
    """An interrupted turn recorded as fact would be repeated on the next turn."""
    link = FakeLink()
    history = History(token_budget=8000)
    loop = ToolLoop(
        link=link,
        provider=FailingProvider("nope"),
        catalog=catalog.load_catalog(),
        history=history,
        max_retries=0,
        retry_base_s=0.0,
    )
    await loop.run(turn_id="t-1", message_id="m1", user_text="change everything")

    assert len(history) == 0


async def test_history_records_a_completed_turn() -> None:
    link = FakeLink()
    history = History(token_budget=8000)
    loop = ToolLoop(
        link=link,
        provider=ScriptedProvider([text_turn("All done.")]),
        catalog=catalog.load_catalog(),
        history=history,
    )
    await loop.run(turn_id="t-1", message_id="m1", user_text="hello")

    roles = [message.role for message in history.messages()]
    assert roles == ["user", "assistant"]


async def test_history_is_trimmed_to_its_budget() -> None:
    history = History(token_budget=40)
    for index in range(50):
        history.append(f"user message {index}", f"assistant reply {index}")

    # Trimmed from the front, so the most recent exchange survives.
    assert len(history) < 50
    assert history.messages()[-1].text == "assistant reply 49"


# -- stale level state ---------------------------------------------------


class RecordingProvider(Provider):
    """Records the messages it is given, and answers nothing in particular."""

    name = "recording"

    def __init__(self) -> None:
        self.seen: list[list[Message]] = []

    async def complete(self, *, system, messages, tools) -> AssistantTurn:  # noqa: A002
        self.seen.append([Message(role=m.role, text=m.text) for m in messages])
        return AssistantTurn(text="ok", stop_reason="end_turn", usage=Usage(1, 1))


async def test_a_hand_edit_is_announced_once_before_the_user_message() -> None:
    """A deleted-objects level must not be answered against the old picture.

    The transcript is kept (closing and reopening a level should not erase the
    conversation), so the only signal the model gets that reality moved is this
    note. It is consumed once, so it does not become permanent noise.
    """
    link = FakeLink()
    history = History(token_budget=8000)
    provider = RecordingProvider()
    loop = ToolLoop(link=link, provider=provider, catalog=catalog.load_catalog(), history=history)

    history.mark_level_changed()
    await loop.run(turn_id="t-1", message_id="m1", user_text="add a platform")

    first = provider.seen[0]
    assert "level changed outside the assistant" in first[0].text
    # The note comes before the message, so it reads as context, not as something
    # the user just said.
    assert first[0].role == "user"
    assert first[1].text == "add a platform"

    # Consumed: the next turn must not repeat it.
    await loop.run(turn_id="t-2", message_id="m2", user_text="and another")
    second = provider.seen[1]
    assert all("level changed outside the assistant" not in m.text for m in second)


async def test_an_unchanged_level_gets_no_note() -> None:
    link = FakeLink()
    provider = RecordingProvider()
    loop = ToolLoop(
        link=link,
        provider=provider,
        catalog=catalog.load_catalog(),
        history=History(token_budget=8000),
    )

    await loop.run(turn_id="t-1", message_id="m1", user_text="hello")

    assert all("level changed" not in m.text for m in provider.seen[0])


def test_history_clear_also_clears_the_change_flag() -> None:
    history = History(token_budget=8000)
    history.mark_level_changed()
    history.clear()
    assert history.take_level_changed() is None


async def test_a_revert_is_described_as_a_revert_not_as_a_hand_edit() -> None:
    """The two causes need different notes, and the revert one is the sharper.

    After a hand edit the level is merely unknown. After a revert the model's own
    transcript says the work succeeded, so "something changed" invites it to defend
    work that no longer exists; the note has to say the work is gone.
    """
    link = FakeLink()
    history = History(token_budget=8000)
    provider = RecordingProvider()
    loop = ToolLoop(link=link, provider=provider, catalog=catalog.load_catalog(), history=history)

    history.mark_level_changed(REVERTED_NOTE)
    await loop.run(turn_id="t-1", message_id="m1", user_text="what did you just do?")

    note = provider.seen[0][0]
    assert note.role == "user"
    assert "undid your last turn" in note.text
    # It must not reuse the ids from the undone work.
    assert "object ids" in note.text


def test_a_note_taken_twice_is_only_delivered_once() -> None:
    history = History(token_budget=8000)
    history.mark_level_changed(REVERTED_NOTE)
    assert history.take_level_changed() == REVERTED_NOTE
    assert history.take_level_changed() is None


def test_a_hand_edit_does_not_downgrade_a_pending_revert_note() -> None:
    """The mod reports both, in that order, for one revert.

    Reverting changes the object count, so the mod's change detector sends a
    `level_changed` right after the `transaction_reverted`. Letting the vaguer note
    win would leave the model free to insist the work was done.
    """
    history = History(token_budget=8000)
    history.mark_level_changed(REVERTED_NOTE)
    history.mark_level_changed()
    assert history.take_level_changed() == REVERTED_NOTE


def test_the_scripted_ground_fallback_is_used_when_the_level_reports_none() -> None:
    from gd_ai_assistant.providers.scripted import default_plan

    plan = default_plan("add a row of spikes", ground_y=None)
    objects = plan[1].tool_calls[0].arguments["objects"]
    assert all(obj["y"] == 15.0 for obj in objects)


def test_the_scripted_provider_places_on_the_levels_real_ground() -> None:
    """The demo must sit on the ground the level reports, not a hard-coded guess.

    This is the bug a user hit: the row was created successfully at y=105 in a level
    whose ground line was higher, so it was buried and invisible -- and the summary
    said "Created 4 objects", which reads as success. Correct data, wrong place.
    """
    from gd_ai_assistant.providers.scripted import default_plan

    plan = default_plan("add a row of spikes", ground_y=255.0)
    objects = plan[1].tool_calls[0].arguments["objects"]
    assert objects, "the plan should create something"
    assert {obj["y"] for obj in objects} == {255.0}


async def test_ground_y_is_read_from_the_editor_state_tool_result() -> None:
    """End to end: the tool result carries ground_y and the plan honours it."""

    class GroundLink(FakeLink):
        async def request(self, type_name, payload=None):
            self.requests.append((type_name, payload or {}))
            if type_name == "get_editor_state":
                return Response(ok=True, result={"object_count": 3, "ground_y": 255.0})
            return Response(ok=True, result={"created": [1, 2, 3], "count": 3})

    link = GroundLink()
    await make_loop(link, ScriptedProvider()).run(
        turn_id="t-1", message_id="m1", user_text="add a row of spikes"
    )

    payload = next(p for name, p in link.requests if name == "create_objects")
    assert {obj["y"] for obj in payload["objects"]} == {255.0}


async def test_the_scripted_provider_plans_every_turn_not_just_the_first() -> None:
    """One provider instance serves every turn of a session, so the plan must reset.

    This was a real bug: `_planned` was computed once and never cleared, so only the
    first turn in the sidecar's lifetime did anything and every later turn answered
    "nothing further planned". A test that builds a fresh provider per turn cannot
    see it -- which is exactly what every other test here does.
    """
    link = FakeLink({"create_objects": {"created": [1, 2, 3], "count": 3}})
    provider = ScriptedProvider()  # one instance, three turns
    loop = make_loop(link, provider)

    for turn in range(1, 4):
        result = await loop.run(
            turn_id=f"t-{turn}", message_id=f"m{turn}", user_text="add a row of spikes"
        )
        assert result.status == COMPLETED
        assert result.actions, f"turn {turn} planned no actions"

    # Every turn created objects, rather than only the first.
    assert link.types().count("create_objects") == 3


# -- schema agreement ----------------------------------------------------


@pytest.mark.parametrize("name", sorted(tools.TOOLS))
def test_every_tool_schema_is_valid_json_schema(name: str) -> None:
    """Providers reject a malformed schema, and some do it by refusing the request."""
    tool = tools.TOOLS[name]
    Draft202012Validator.check_schema(tool.spec.parameters)


def test_every_mutating_tool_maps_to_a_protocol_request() -> None:
    """A mutating tool either is a mod request or is built from the mutating ones."""
    for tool in tools.TOOLS.values():
        if tool.mutates_objects or tool.is_action:
            builds = "create_objects" in tool.requires
            assert tool.protocol_type is not None or builds, tool.spec.name


def test_the_prompt_lists_exactly_the_settable_properties() -> None:
    """A prompt that disagrees with the schema is worse than no prompt."""
    from gd_ai_assistant.agent_prompt import system_prompt

    text = system_prompt()
    for name in tools.SETTABLE_PROPERTIES:
        assert f"`{name}`" in text, name


def test_colour_is_read_and_written_through_a_channel() -> None:
    """Colour is a channel id, and both directions go through the channel.

    Two things here were wrong in earlier versions and both are worth remembering.
    Reading reported `ColorAction::m_fromColor`, which is an animation's *start*
    colour rather than the stored one, so every channel read as white and a user's
    own colour edits were invisible to the model.

    Writing targeted `GJEffectManager`'s colour state, which is derived: `ColorAction`
    is an animation (`step(dt)`, `m_duration`, `m_fromColor` -> `m_toColor`), so a
    written `m_color` is recomputed a frame later. The durable store is the level's
    `kS38` colour section, which is why the write goes through the game's own setup
    string format rather than through that state.
    """
    from gd_ai_assistant.agent_prompt import system_prompt

    create = tools.TOOLS["create_objects"]
    objects = create.spec.parameters["properties"]["objects"]
    settable = objects["items"]["properties"]["properties"]["properties"]
    assert "color_channel_1" in settable
    # Both directions of the channel are offered.
    assert "set_color_channel" in tools.TOOLS
    assert "get_color_channel" in tools.TOOLS

    text = system_prompt()
    assert "channel id" in text
    # The two facts the model has to state out loud: one channel drives many
    # objects, and the system channels share the same id space.
    assert "borrow" in text
    assert "1000" in text
