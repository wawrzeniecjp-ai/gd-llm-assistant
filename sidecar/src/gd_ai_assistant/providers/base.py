"""Provider-neutral conversation types and the adapter interface (SPEC.md 6.2.3).

D5 requires *native* function calling, and the providers disagree about what that
looks like: Anthropic returns content blocks with a `tool_use` type and a
JSON-object `input`, while OpenAI returns `tool_calls` with a JSON *string* in
`function.arguments`. Normalising in an adapter means the tool loop, the caps and
the tests never see either shape -- and it is what makes the loop testable at all,
because the scripted provider implements the same tiny interface.

The conversation is modelled as flat messages rather than a tree of content
blocks. That is enough for v1 (text plus tool calls) and it keeps every adapter's
translation obvious:

    user       text
    assistant  text + tool_calls
    tool       tool_call_id + text (+ is_error)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]

#: How the model ended its turn. `tool_use` means it wants results and the loop
#: should keep going; anything else ends the turn.
StopReason = Literal["end_turn", "tool_use", "max_tokens"]


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the model sees it."""

    name: str
    description: str
    parameters: dict[str, Any]
    """JSON Schema for the arguments. Providers pass this through unchanged."""


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    """Provider-assigned id, echoed back with the result so the model can match
    them up when it sees several calls in one turn."""

    name: str
    arguments: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class Message:
    """One entry of the conversation history."""

    role: Role
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    """Set for `role == "tool"`: which call this is the result of."""

    images: list[str] = field(default_factory=list)
    """`data:` URLs attached to a **user** message.

    User messages specifically, not tool results: the providers document that images are
    accepted in `user` messages and rejected in `system`/`assistant` ones, and a tool result
    has a role of its own that the documentation does not cover. Appending a user message
    when a tool produces a picture keeps this inside the documented contract rather than
    depending on how a tool role happens to be treated.
    """

    is_error: bool = False
    """Set for `role == "tool"`: the tool failed, and the text says how.

    Reported to the model as a tool result rather than raised, because a failed
    tool call is information the model can act on -- a mistyped object id is
    exactly what it should be told about, so it can correct itself or explain.
    """


@dataclass
class Usage:
    """Token accounting, reported in `turn_finished` (SPEC.md 6.2.3, 12)."""

    input_tokens: int = 0
    output_tokens: int = 0
    tool_rounds: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            tool_rounds=self.tool_rounds + other.tool_rounds,
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_json(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_rounds": self.tool_rounds,
        }


@dataclass
class AssistantTurn:
    """One model response."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ProviderError(Exception):
    """A provider call failed in a way the turn should report to the user.

    `retryable` drives the bounded backoff (SPEC.md 6.2.3): 429 and 5xx are worth
    retrying, a 400 for a malformed request is not, and retrying it just adds
    latency to an error the user needs to see.
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ProviderUnavailable(Exception):
    """No provider is configured at all (SPEC.md 8.8 `needs_setup`).

    Distinct from `ProviderError` because it is a setup problem rather than a
    failure: the turn never starts, and the panel should say what to fix.
    """


class Provider(ABC):
    """The interface the tool loop depends on."""

    name: str = "unknown"

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AssistantTurn:
        """Produce the next assistant turn.

        Implementations must raise `ProviderError` rather than leaking an SDK
        exception: the loop reports provider failures to the user, so they have to
        arrive as one known type with a message already fit to show.
        """
