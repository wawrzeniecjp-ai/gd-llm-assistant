"""Deterministic provider: the fake LLM that makes the loop testable (SPEC.md 13).

Two jobs, and they are genuinely different, which is why both live here:

* **A test vehicle.** Tests construct `ScriptedProvider(turns=[...])` with an exact
  sequence of tool calls and final text, then assert what the loop did with them.
  Nothing is inferred, nothing is random, no network and no cost -- which is what
  makes it possible to test caps, cancellation and error paths at all.
* **An in-editor demo.** With `llm_provider=scripted` and no API key, the panel
  still works end to end: `default_plan` derives a small plausible turn from the
  user's message, so the panel can be exercised without a provider account.

Determinism is the entire point, so nothing here may depend on process state:

* `hash()` is **not** used anywhere. Python randomises string hashing per process
  (PYTHONHASHSEED), so the same message would produce a different turn on every
  run and any test asserting on it would be flaky in a way that looks like a real
  bug in the loop.
* `zlib.crc32` is used instead: stable across processes and versions, and good
  enough to vary the reply so a round trip reusing a stale message is visible.
  Nothing depends on it being a good hash.
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import Callable, Iterable
from typing import Any

from .base import AssistantTurn, Message, Provider, ProviderError, ToolCall, ToolSpec, Usage

#: Nouns the demo plan recognises, so the reply mentions something plausible.
_DEMO_BLOCKS = ("spike", "block", "platform", "saw", "jump pad", "orb")

#: Object type ids for the demo. GD's 1.0 "spike" is id 8, the plain block 1.
#: Fixed rather than looked up: the scripted provider cannot see tool results, so
#: it cannot consult the catalog it is about to "search".
_DEMO_SPIKE_ID = 8

#: Demo placement. A row on the ground at GD's default ground height, in 30-unit
#: grid steps from x=300. Fixed for the same reason as the id.
_DEMO_START_X = 300.0
_DEMO_GROUND_Y = 105.0
_DEMO_GRID = 30.0


def _seed(text: str) -> int:
    """A stable non-negative integer derived from `text` (see the module docstring)."""
    return zlib.crc32(text.encode("utf-8"))


def _last_user_text(messages: Iterable[Message]) -> str:
    for message in reversed(list(messages)):
        if message.role == "user":
            return message.text
    return ""


class ScriptedProvider(Provider):
    """Replays a fixed sequence of turns, or a plan derived from the message."""

    name = "scripted"

    def __init__(
        self,
        turns: list[AssistantTurn] | None = None,
        *,
        delay_ms: int = 0,
        plan: Callable[[str], list[AssistantTurn]] | None = None,
    ) -> None:
        self._turns = list(turns) if turns is not None else None
        self._plan = plan or default_plan
        self._delay_ms = delay_ms
        self._call_counter = 0
        self._rounds = 0
        self._planned: list[AssistantTurn] | None = None

    def next_call_id(self) -> str:
        self._call_counter += 1
        return f"call-{self._call_counter}"

    def tool_call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCall:
        """Build a call with a provider-unique id. For tests and `default_plan`."""
        return ToolCall(id=self.next_call_id(), name=name, arguments=arguments or {})

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AssistantTurn:
        self._rounds += 1

        if self._delay_ms:
            # The only await point, and therefore the only place a `cancel_turn`
            # can land. Deliberate: the scripted provider exists partly to test
            # cancellation, and a turn that could not be interrupted would test
            # nothing.
            await asyncio.sleep(self._delay_ms / 1000)

        if self._turns is not None:
            if self._turns:
                return self._turns.pop(0)
            return self._final_turn("The scripted provider has nothing further planned.")

        if self._planned is None:
            self._planned = list(self._plan(_last_user_text(messages)))

        if self._planned:
            return self._planned.pop(0)

        return self._final_turn(
            "The scripted provider has nothing further planned. "
            "Set a real provider to get an actual answer."
        )

    def _final_turn(self, text: str) -> AssistantTurn:
        return AssistantTurn(
            text=text,
            stop_reason="end_turn",
            # A rough estimate rather than zero, so token accounting and the
            # per-turn budget are exercised on the scripted path too. An
            # implementation that reports nothing here would leave the budget
            # untested until the first real provider was wired up.
            usage=Usage(input_tokens=64, output_tokens=max(1, len(text) // 4)),
        )


def default_plan(user_text: str) -> list[AssistantTurn]:
    """A small turn derived from `user_text`, for use without a real provider.

    Read-only by default, and it only plans a mutation when the message names
    something it knows how to place. That is deliberate: a demo provider that
    edits the user's level unasked is worse than one that only surveys it, and the
    read-only path still exercises the interesting machinery (tool dispatch, the
    round cap, the summary).
    """
    seed = _seed(user_text)
    lowered = user_text.lower()
    wanted = next((name for name in _DEMO_BLOCKS if name in lowered), None)

    calls: list[ToolCall] = [
        # Ids are assigned per provider instance by `ScriptedProvider.tool_call`,
        # but `default_plan` is a free function so it builds them literally. The
        # prefix keeps them unique within a conversation, which is all the
        # providers require.
        ToolCall(id=f"plan-{seed}-1", name="get_editor_state", arguments={}),
        ToolCall(
            id=f"plan-{seed}-2",
            name="search_object_catalog",
            arguments={"query": wanted or "spike"},
        ),
    ]

    if wanted is None:
        return [
            AssistantTurn(text="", tool_calls=calls, stop_reason="tool_use", usage=Usage(120, 40)),
            AssistantTurn(
                text=(
                    "I looked at the level and the object catalog. "
                    f"You asked: {user_text}\n"
                    "Nothing was changed. This is the scripted provider: name a "
                    "block type (spike, block, platform, saw) and I will place a "
                    "small row of them to demonstrate the edit path."
                ),
                stop_reason="end_turn",
                usage=Usage(200, 60),
            ),
        ]

    count = 3 + seed % 3  # 3..5
    objects = [
        {
            "id": _DEMO_SPIKE_ID,
            "x": _DEMO_START_X + index * _DEMO_GRID,
            "y": _DEMO_GROUND_Y,
        }
        for index in range(count)
    ]

    return [
        AssistantTurn(text="", tool_calls=calls, stop_reason="tool_use", usage=Usage(120, 40)),
        AssistantTurn(
            text="",
            tool_calls=[
                ToolCall(
                    id=f"plan-{seed}-3",
                    name="create_objects",
                    arguments={"objects": objects},
                )
            ],
            stop_reason="tool_use",
            usage=Usage(160, 50),
        ),
        AssistantTurn(
            text=(
                f"Placed {count} {wanted}s along the ground starting at x={_DEMO_START_X:g}. "
                "This is the scripted provider, so the placement is a fixed demo rather "
                "than a response to the level."
            ),
            stop_reason="end_turn",
            usage=Usage(220, 70),
        ),
    ]


class FailingProvider(Provider):
    """Always fails, for testing the error path (SPEC.md 11).

    `retryable` selects between the two behaviours the loop must handle
    differently: a transparent bounded retry, and a failure reported straight
    away.
    """

    name = "failing"

    def __init__(self, message: str = "provider exploded", *, retryable: bool = False) -> None:
        self.message = message
        self.retryable = retryable
        self.calls = 0

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AssistantTurn:
        self.calls += 1
        status = 429 if self.retryable else 400
        raise ProviderError(self.message, retryable=self.retryable, status=status)
