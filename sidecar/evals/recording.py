"""Providers for the harness: one that records what a real model did, one that replays it.

A recording is the model's turns, in order, as JSON. Replaying it against the same case
gives the same run, because the fake editor and the tools are deterministic; that is how
the harness itself is tested in CI without a model (LEVEL-DESIGN-SPEC.md 15.5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gd_ai_assistant.providers.base import (
    AssistantTurn,
    Message,
    Provider,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)


def turn_to_json(turn: AssistantTurn) -> dict[str, Any]:
    return {"text": turn.text, "tool_calls": [c.to_json() for c in turn.tool_calls],
            "stop_reason": turn.stop_reason,
            "usage": {"input_tokens": turn.usage.input_tokens,
                      "output_tokens": turn.usage.output_tokens}}


def turn_from_json(data: dict[str, Any]) -> AssistantTurn:
    usage = data.get("usage") or {}
    return AssistantTurn(
        text=data.get("text", ""),
        tool_calls=[ToolCall(c["id"], c["name"], c.get("arguments") or {})
                    for c in data.get("tool_calls") or []],
        stop_reason=data.get("stop_reason", "end_turn"),
        usage=Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))),
    )


@dataclass
class ToolEvent:
    """One tool call the model made, and whether its result was an error."""

    name: str
    arguments: dict[str, Any]
    error: bool = False
    result: str = ""


@dataclass
class Recording:
    turns: list[dict[str, Any]] = field(default_factory=list)
    tools: list[ToolEvent] = field(default_factory=list)


class RecordingProvider(Provider):
    """Wraps a provider; keeps every turn it returns and every tool result it is shown."""

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.name = inner.name
        self.recording = Recording()
        self._pending: dict[str, ToolEvent] = {}

    async def complete(self, *, system: str, messages: list[Message],
                       tools: list[ToolSpec]) -> AssistantTurn:
        # The results of the calls made last round are in `messages` now.
        for message in messages:
            if message.role == "tool" and message.tool_call_id in self._pending:
                event = self._pending.pop(message.tool_call_id)
                event.error = message.is_error
                event.result = message.text[:2000]
        turn = await self.inner.complete(system=system, messages=messages, tools=tools)
        self.recording.turns.append(turn_to_json(turn))
        for call in turn.tool_calls:
            event = ToolEvent(call.name, call.arguments)
            self.recording.tools.append(event)
            self._pending[call.id] = event
        return turn

    def flush(self, messages: list[Message] | None = None) -> None:
        """Results of the last round, which no later call ever saw."""
        for message in messages or []:
            if message.role == "tool" and message.tool_call_id in self._pending:
                event = self._pending.pop(message.tool_call_id)
                event.error = message.is_error


class ReplayProvider(Provider):
    """Plays back the turns of a recording, in order."""

    name = "replay"

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)

    @classmethod
    def from_file(cls, path: Path) -> ReplayProvider:
        return cls(json.loads(path.read_text(encoding="utf-8"))["turns"])

    async def complete(self, *, system: str, messages: list[Message],
                       tools: list[ToolSpec]) -> AssistantTurn:
        if not self._turns:
            raise ProviderError("the recording has no more turns (the run diverged)")
        return turn_from_json(self._turns.pop(0))
