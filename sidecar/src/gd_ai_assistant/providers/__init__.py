"""Provider selection (SPEC.md 6.2.2, 9.2).

Imports are deferred into the factory rather than done at module scope. The
scripted provider is the one that has to work everywhere -- in tests and in a
first run with no API key -- and importing the vendor SDKs eagerly would make the
sidecar depend on both being installed just to start in that mode.
"""

from __future__ import annotations

import logging

from ..config import SidecarConfig
from .base import (
    AssistantTurn,
    Message,
    Provider,
    ProviderError,
    ProviderUnavailable,
    ToolCall,
    ToolSpec,
    Usage,
)
from .scripted import FailingProvider, ScriptedProvider, default_plan

log = logging.getLogger("sidecar")

__all__ = [
    "AssistantTurn",
    "FailingProvider",
    "Message",
    "Provider",
    "ProviderError",
    "ProviderUnavailable",
    "ScriptedProvider",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "create_provider",
    "default_plan",
]


def create_provider(config: SidecarConfig) -> Provider:
    """Build the configured provider.

    Raises `ProviderUnavailable` when nothing usable is configured, which the
    server turns into `state: needs_setup` (SPEC.md 8.8) rather than a turn
    failure -- it is a setup problem with a user-facing fix, not an error.
    """
    name = config.llm_provider

    if name == "scripted":
        log.info("provider: scripted (no network calls will be made)")
        return ScriptedProvider(delay_ms=config.scripted_turn_delay_ms)

    if name == "anthropic":
        if not config.api_key:
            raise ProviderUnavailable("Anthropic is selected but no api_key is configured.")
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )

    if name in ("openai", "openai_compatible"):
        if not config.api_key:
            raise ProviderUnavailable("OpenAI is selected but no api_key is configured.")
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=config.api_key,
            model=config.model,
            base_url=config.base_url,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            log_reasoning=config.log_reasoning,
        )

    raise ProviderUnavailable(
        f"No LLM provider is configured (llm_provider={name!r}). "
        "Set llm_provider, model and api_key; use 'scripted' to run without a provider."
    )
