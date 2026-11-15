"""OpenAI adapter, also used for OpenAI-compatible endpoints (SPEC.md 6.2.2).

Three differences from the Anthropic adapter are worth knowing, because each is a
place where a naive translation produces a confusing runtime failure:

* **Tool arguments are a JSON string.** `function.arguments` arrives as text the
  model produced, so it can be malformed. That is not hypothetical -- a truncated
  response is exactly this -- and it must become a failed tool result the model
  can recover from, not an exception that kills the turn.
* **Tool results are a `tool` role with `tool_call_id`.** Different from
  Anthropic's `tool_result` block. Both are handled here so the loop stays neutral.
* **`base_url` makes it speak to any OpenAI-compatible endpoint**, which is how
  local runners are supported. That is the only difference between the `openai`
  and `openai_compatible` providers, so they share this class.
"""

from __future__ import annotations

import json
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

_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


def _to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    """Convert neutral messages to OpenAI's shape."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]

    for message in messages:
        if message.role == "user":
            if message.images:
                # A content array rather than a plain string is what carries images. Text
                # first, then each picture: the model reads the instruction before the
                # image it refers to, which matters when a turn produces several.
                blocks: list[dict[str, Any]] = [{"type": "text", "text": message.text}]
                blocks.extend(
                    {"type": "image_url", "image_url": {"url": url}} for url in message.images
                )
                out.append({"role": "user", "content": blocks})
            else:
                out.append({"role": "user", "content": message.text})

        elif message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            out.append(entry)

        elif message.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": message.text,
                }
            )

    return out


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str | None,
        base_url: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        log_reasoning: bool = False,
    ) -> None:
        import openai

        # `base_url=None` means the SDK's own default, so only pass it when set:
        # passing None explicitly is not the same as omitting it for some
        # compatible endpoints.
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)
        self._model = model or "gpt-4o-mini"
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._log_reasoning = log_reasoning
        self.name = "openai_compatible" if base_url else "openai"

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
            "messages": _to_openai_messages(system, messages),
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]

        try:
            response = await self._client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001 - every failure must become a ProviderError
            raise _as_provider_error(exc) from exc

        choice = response.choices[0]
        message = choice.message

        calls: list[ToolCall] = []
        for raw in message.tool_calls or []:
            calls.append(
                ToolCall(
                    id=raw.id,
                    name=raw.function.name,
                    arguments=_parse_arguments(raw.function.arguments, raw.function.name),
                )
            )

        # `tool_calls` is the stop reason that means "keep going". Anything else
        # ends the turn, including `length`, which is worth surfacing because a
        # truncated turn looks like a model that stopped mid-sentence.
        # Thinking models (DeepSeek) return their reasoning beside the answer, in a field
        # the SDK does not model; it is never sent back or shown in the panel.
        extra = getattr(message, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content") or ""
        if reasoning and self._log_reasoning:
            log.info("reasoning (%d chars):\n%s", len(reasoning), _indent(reasoning))
        if not calls and not (message.content or "").strip():
            # An empty answer ends the turn as a bare "Done." in the panel. Say what
            # the endpoint did send, in case it put its text in the reasoning.
            log.warning("%s returned no text and no tool calls (finish_reason %s, "
                        "%d chars of reasoning_content%s)", self._model, choice.finish_reason,
                        len(reasoning), f": {reasoning[-300:]!r}" if reasoning else "")
        if calls:
            stop = "tool_use"
        elif choice.finish_reason == "length":
            stop = "max_tokens"
        else:
            stop = "end_turn"

        usage = Usage(
            input_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "completion_tokens", 0) or 0,
        )

        return AssistantTurn(
            text=(message.content or "").strip(),
            tool_calls=calls,
            stop_reason=stop,  # type: ignore[arg-type]
            usage=usage,
        )


def _indent(text: str) -> str:
    """`text` indented under its log line, so a block of reasoning reads as one entry."""
    return "\n".join("    | " + line for line in text.strip().splitlines())


def _parse_arguments(raw: str | None, tool_name: str) -> dict[str, Any]:
    """Decode `function.arguments`, which the model wrote as text.

    On malformed JSON an empty argument set is returned *and* logged, so the tool
    call still happens and fails its own validation with a message the model can
    act on. Raising instead would end the turn over a recoverable mistake.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("tool %s: could not parse arguments (%s): %r", tool_name, exc, raw[:200])
        return {}
    if not isinstance(parsed, dict):
        log.warning("tool %s: arguments were %s, not an object", tool_name, type(parsed).__name__)
        return {}
    return parsed


def _as_provider_error(exc: Exception) -> ProviderError:
    """Map an SDK exception to a `ProviderError` with a message fit to display."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)

    retryable = status in _RETRYABLE_STATUS
    if status == 401:
        return ProviderError(
            "The API key was rejected. Check the key in the sidecar configuration.",
            retryable=False,
            status=status,
        )
    if status == 404:
        return ProviderError(
            "The provider does not recognise that model name. Check `model` in the "
            "sidecar configuration.",
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

    message = getattr(exc, "message", None) or str(exc)
    return ProviderError(f"Provider request failed: {message}", retryable=retryable, status=status)
