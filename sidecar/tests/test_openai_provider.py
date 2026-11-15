"""The OpenAI-compatible provider: what it makes of a response (no network)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from gd_ai_assistant.providers.openai_provider import OpenAIProvider


class _Completions:
    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response

    async def create(self, **_request) -> SimpleNamespace:
        return self.response


def _provider(message: SimpleNamespace, finish_reason: str = "stop", **kwargs) -> OpenAIProvider:
    provider = OpenAIProvider(api_key="test", model="m", base_url="http://127.0.0.1:1", **kwargs)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    provider._client = SimpleNamespace(  # noqa: SLF001 - replacing the network client
        chat=SimpleNamespace(completions=_Completions(response)))
    return provider


def _message(content: str, reasoning: str | None) -> SimpleNamespace:
    extra = {"reasoning_content": reasoning} if reasoning is not None else {}
    return SimpleNamespace(content=content, tool_calls=None, model_extra=extra)


@pytest.mark.parametrize("enabled", [True, False])
async def test_reasoning_is_logged_only_when_enabled(caplog, enabled: bool) -> None:
    provider = _provider(_message("Placed it.", "First the stair,\nthen the orb."),
                         log_reasoning=enabled)
    with caplog.at_level(logging.INFO, logger="sidecar"):
        turn = await provider.complete(system="s", messages=[], tools=[])
    assert turn.text == "Placed it."
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert ("    | then the orb." in logged) is enabled


async def test_an_empty_answer_is_a_warning_that_quotes_the_reasoning(caplog) -> None:
    provider = _provider(_message("", "row 0 = '....'"), finish_reason="length")
    with caplog.at_level(logging.INFO, logger="sidecar"):
        turn = await provider.complete(system="s", messages=[], tools=[])
    assert turn.stop_reason == "max_tokens"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "finish_reason length" in warnings[0]
