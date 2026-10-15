"""Anthropic adapter (SPEC.md 6.2.2, D5).

The translation is small but not trivial, and the interesting parts are:

* **Tool results are user messages.** Anthropic has no `tool` role; a result is a
  `tool_result` content block inside a `user` message. Getting this wrong produces
  a 400 that names the role, which is at least a clear failure.
* **`input` is already an object.** Anthropic parses the arguments JSON for us, so
  unlike the OpenAI adapter there is no string to decode -- and therefore no new
  failure mode when the model emits malformed JSON.
* **Errors must be mapped.** SPEC.md 11 requires provider errors to reach the user
  as readable text with the key redacted, so a raw SDK exception must never escape
  this class.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import (
    AssistantTurn,
    Message,
    Provider,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)

log = logging.getLogger("sidecar")

#: Statuses worth a bounded retry (SPEC.md 6.2.3): overloaded/rate-limited and
#: server-side faults. Anything else is a request the retry would repeat verbatim.
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert neutral messages to Anthropic's shape."""
    out: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "user":
            out.append({"role": "user", "content": message.text})

        elif message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.text:
                blocks.append({"type": "text", "text": message.text})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            # An assistant message with neither text nor calls is skipped rather
            # than sent empty: Anthropic rejects an empty content array, and an
            # empty assistant turn carries no information anyway.
            if blocks:
                out.append({"role": "assistant", "content": blocks})

        elif message.role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id or "",
                            "content": message.text,
                            "is_error": message.is_error,
                        }
                    ],
                }
            )

    return out


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model or "claude-sonnet-4-20250514"
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
    ) -> AssistantTurn:
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "system": system,
            "messages": _to_anthropic_messages(messages),
        }
        if tools:
            request["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]

        try:
            response = await self._client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 - every failure must become a ProviderError
            raise _as_provider_error(exc) from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                # `block.input` is a mapping by contract; a non-mapping would mean
                # the SDK changed under us, so fall back rather than crash.
                arguments = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))

        stop_reason = response.stop_reason
        if calls:
            stop = "tool_use"
        elif stop_reason == "max_tokens":
            stop = "max_tokens"
        else:
            stop = "end_turn"

        usage = Usage(
            input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
        )

        return AssistantTurn(
            text="\n".join(part for part in text_parts if part).strip(),
            tool_calls=calls,
            stop_reason=stop,  # type: ignore[arg-type]
            usage=usage,
        )


def _as_provider_error(exc: Exception) -> ProviderError:
    """Map an SDK exception to a `ProviderError` with a message fit to display."""
    status = getattr(exc, "status_code", None)
    if status is None:
        # Some SDK failures carry the status on a nested response object.
        status = getattr(getattr(exc, "response", None), "status_code", None)

    retryable = status in _RETRYABLE_STATUS
    if status == 401:
        return ProviderError(
            "The API key was rejected. Check the key in the sidecar configuration.",
            retryable=False,
            status=status,
        )
    if status == 429:
        return ProviderError(
            "The provider is rate limiting us. Try again shortly.", retryable=True, status=status
        )
    if isinstance(status, int) and status >= 500:
        return ProviderError(
            f"The provider reported a server error ({status}).", retryable=True, status=status
        )

    # Fall back to the SDK's own message, which is written for a human, but do not
    # include the request body: it contains level data.
    message = getattr(exc, "message", None) or str(exc)
    return ProviderError(f"Provider request failed: {message}", retryable=retryable, status=status)
