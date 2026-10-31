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
import json
import zlib
from collections.abc import Callable, Iterable
from typing import Any

from .base import AssistantTurn, Message, Provider, ProviderError, ToolCall, ToolSpec, Usage

#: Nouns the demo plan recognises, so the reply mentions something plausible.
_DEMO_BLOCKS = ("spike", "block", "platform", "saw", "jump pad", "orb")

#: Object type ids for the demo. GD's 1.0 "spike" is id 8, the plain block 1.
#: Fixed rather than looked up: the scripted provider reads tool results only for the
#: ground line, so it cannot consult the catalog it is about to "search".
_DEMO_SPIKE_ID = 8

#: Demo placement: a row in 30-unit grid steps from x=300.
_DEMO_START_X = 300.0
_DEMO_GRID = 30.0

#: Fallback ground height, used only when `get_editor_state` did not report one.
#:
#: It used to be the *only* value, and the result was a row buried under the ground
#: in a level whose ground line sits higher: created correctly, reported correctly,
#: and invisible. The real height now comes from the `ground_y` field of the last
#: `get_editor_state` result, which is what `default_plan` reads.
_DEMO_GROUND_Y_FALLBACK = 105.0


def _seed(text: str) -> int:
    """A stable non-negative integer derived from `text` (see the module docstring)."""
    return zlib.crc32(text.encode("utf-8"))


def _last_user_text(messages: Iterable[Message]) -> str:
    for message in reversed(list(messages)):
        if message.role == "user":
            return message.text
    return ""


def _ground_y_from(messages: Iterable[Message]) -> float | None:
    """The level's ground line, read from the most recent `get_editor_state` result.

    The demo used to hard-code a ground height, and in a level whose ground line sits
    higher the row it created was buried and invisible. Reading it from the tool
    result is the difference between an edit the user can see and one they cannot.

    Parsed defensively: a tool result is JSON the loop produced, but a malformed or
    older one should degrade to the fallback rather than raise inside a turn.
    """
    for message in reversed(list(messages)):
        if message.role != "tool":
            continue
        try:
            payload = json.loads(message.text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("ground_y"), (int, float)):
            return float(payload["ground_y"])
    return None


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
        #: Which demo phase the next call should produce.
        self._phase = 0
        self._phase_done = False

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

        # A new turn begins when the last message in the conversation is the user's.
        #
        # The plan used to be computed once and never reset, so only the first turn
        # in the sidecar's lifetime did anything and every later turn answered
        # "nothing further planned". Invisible in a test that runs one turn per
        # provider instance, obvious the moment a second message is sent.
        if self._phase_done or (messages and messages[-1].role == "user"):
            self._phase = 0
            self._phase_done = False

        # One turn at a time, not a precomputed list: the create step needs the
        # `ground_y` from the `get_editor_state` result, which does not exist yet
        # when the plan is first consulted. Precomputing the whole plan silently
        # used the fallback ground height and buried the row under the ground.
        turn = default_turn(
            _last_user_text(messages), self._phase, _ground_y_from(messages)
        )
        if turn is None:
            self._phase_done = True
            return self._final_turn(
                "The scripted provider has nothing further planned. "
                "Set a real provider to get an actual answer."
            )

        self._phase += 1
        return turn

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


def default_turn(
    user_text: str, phase: int, ground_y: float | None = None
) -> AssistantTurn | None:
    """The demo turn for `phase`, or None when the turn is over.

    Phases rather than a precomputed list because the create step depends on what
    `get_editor_state` returned, which is not known until that call completes.
    """
    seed = _seed(user_text)
    wanted = next((name for name in _DEMO_BLOCKS if name.lower() in user_text.lower()), None)

    if phase == 0:
        return AssistantTurn(
            text="",
            tool_calls=[
                ToolCall(id=f"plan-{seed}-1", name="get_editor_state", arguments={}),
                ToolCall(
                    id=f"plan-{seed}-2",
                    name="search_object_catalog",
                    arguments={"query": wanted or "spike"},
                ),
            ],
            stop_reason="tool_use",
            usage=Usage(120, 40),
        )

    if wanted is None:
        # Read-only: only say so, and only once.
        if phase != 1:
            return None
        return AssistantTurn(
            text=(
                f"You asked: {user_text}\n"
                "Nothing was changed. This is the scripted provider: name a block type "
                "(spike, block, platform, saw) and I will place a small row of them to "
                "demonstrate the edit path."
            ),
            stop_reason="end_turn",
            usage=Usage(200, 60),
        )

    count = 3 + seed % 3  # 3..5
    # Sit on the level's actual ground line, read from the editor-state result.
    ground = ground_y if ground_y is not None else _DEMO_GROUND_Y_FALLBACK

    if phase == 1:
        objects = [
            {
                "id": _DEMO_SPIKE_ID,
                "x": _DEMO_START_X + index * _DEMO_GRID,
                "y": ground,
            }
            for index in range(count)
        ]
        return AssistantTurn(
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
        )

    if phase == 2:
        return AssistantTurn(
            text=(
                f"Placed {count} {wanted}s along the ground at y={ground:g}, "
                f"starting at x={_DEMO_START_X:g}. "
                "This is the scripted provider, so the placement is a fixed demo rather "
                "than a response to the level."
            ),
            stop_reason="end_turn",
            usage=Usage(220, 70),
        )

    return None


def default_plan(user_text: str, ground_y: float | None = None) -> list[AssistantTurn]:
    """All demo turns for one request, for tests that want the whole sequence."""
    turns: list[AssistantTurn] = []
    for phase in range(8):
        turn = default_turn(user_text, phase, ground_y)
        if turn is None:
            break
        turns.append(turn)
    return turns


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
