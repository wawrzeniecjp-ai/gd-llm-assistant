"""Sidecar configuration (SPEC.md 9.2).

Settings load from environment variables, a `.env` file, or a config file, in
that order of precedence. `max_tokens`/`model`/`api_key` have no defaults on
purpose: the spec requires an explicit, current model choice rather than a
stale built-in default.

Nothing here is ever sent to the mod. `Mod` settings (SPEC.md 6.1.8) remain
authoritative for the limits the mod enforces (D10); the values below must be
less than or equal to the mod's.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _blank_to_none(value: Any) -> Any:
    """Map an empty or whitespace-only string to `None`.

    A dotenv file cannot express "absent" -- `KEY=` is the only way to leave a
    value for the user to fill in -- and pydantic reads that as the empty string,
    not as unset. Without this, the `.env.example` this project ships crashes the
    sidecar on first run:

        llm_provider=''  -> literal_error (not one of the allowed providers)
        turn_token_budget='' -> int_parsing (not an integer)

    Every optional setting goes through here so `KEY=` behaves the way a user
    expects, which is also how every other dotenv-based tool treats it.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


#: An optional setting where `KEY=` in `.env` means "not set".
BlankAsNone = BeforeValidator(_blank_to_none)


def _is_loopback(host: str) -> bool:
    """True for loopback IPs and localhost-style names.

    Deliberately conservative: an unrecognised hostname is treated as
    non-loopback so it is refused unless explicitly opted into.
    """
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _default_token_path() -> Path:
    """%LOCALAPPDATA%/gd-ai-assistant/token on Windows (SPEC.md 7.2)."""
    import os

    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return base / "gd-ai-assistant" / "token"


def _env_file() -> str | None:
    """The dotenv file to load, or None to ignore it.

    `GD_AI_ASSISTANT_IGNORE_ENV_FILE` exists so the test suite is independent of the
    developer's local configuration. Without it, having a real provider configured in
    `sidecar/.env` makes the tests that assert the `needs_setup` state fail -- a suite
    whose result depends on whose machine it runs on.
    """
    import os

    if os.environ.get("GD_AI_ASSISTANT_IGNORE_ENV_FILE"):
        return None
    return ".env"


class SidecarConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM provider -------------------------------------------------
    # `scripted` is a development provider (SPEC.md 13): it replays a
    # deterministic turn with no network, no API key and no cost, which is what
    # makes the Phase 3 exit criterion -- "panel works end-to-end against the
    # fake sidecar" -- testable, and what the cancellation and error paths can be
    # exercised against in Phase 4.
    llm_provider: Annotated[
        Literal["anthropic", "openai", "openai_compatible", "scripted"] | None, BlankAsNone
    ] = None
    api_key: Annotated[str | None, BlankAsNone] = None
    model: Annotated[str | None, BlankAsNone] = None
    base_url: Annotated[str | None, BlankAsNone] = None
    # Output tokens per model call. Thinking models (DeepSeek, OpenAI o-series) count their
    # reasoning here too, and a GridText section can take thousands: 4096 cut a 60-column
    # build off mid-thought (2026-11-14). 16384 is accepted by every supported provider.
    max_tokens: int = 16384
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    # Print what a thinking model reasoned (DeepSeek's `reasoning_content`) to the log,
    # one block per model call, to see what it does between answers.
    log_reasoning: bool = True

    # How long the scripted provider pretends to think before answering. Long
    # enough to see the thinking indicator, short enough not to make the
    # in-editor test cycle tedious. Tests set it to 0.
    scripted_turn_delay_ms: int = Field(default=600, ge=0, le=30000)

    # --- Transport ----------------------------------------------------
    # `allow_non_loopback` must precede `ws_host` only if the check were a
    # field validator; it is a model validator instead, so order is free.
    ws_host: str = "127.0.0.1"
    ws_port: int = Field(default=18765, ge=1, le=65535)
    allow_non_loopback: bool = False
    token_path: Path = Field(default_factory=_default_token_path)

    # Phase 1 verification aid: after a successful handshake, issue `ping` and
    # `get_editor_state` and log the results. Off by default; the real client
    # drives requests from the editor lifecycle instead.
    probe_on_connect: bool = False

    # --- Tool loop limits (SPEC.md 6.2.3, 10.2) -----------------------
    #
    # 40 rather than 10, because 10 was hit by ordinary work. A single request like
    # "find a thin spike and put a row of them above the spikes" needs a catalog
    # search, an editor-state read, an object-type listing, then create and verify --
    # and a model that checks its own work doubles that. Running out mid-plan is
    # worse than being slow: the turn ends INCOMPLETE having half-edited the level,
    # and the user has to work out what happened.
    #
    # The cap is a runaway guard, not a budget. The real spend controls are
    # `turn_token_budget` and `max_objects_per_turn`, both of which still apply, plus
    # `turn_timeout_s` below.
    max_tool_rounds: int = Field(default=40, ge=1, le=200)
    # Bounded by the mod's hard maximum: the mod's caps are authoritative (D10).
    max_objects_per_turn: int = Field(default=5000, ge=1, le=20000)
    turn_token_budget: Annotated[int | None, BlankAsNone] = None
    # Fractional so that tests can set a timeout small enough to exercise the
    # timeout path without a slow test; a whole-second floor would make the
    # shortest meaningful case a one-second wait.
    #
    # Raised with the round cap: 40 rounds of a tool-using model is several minutes
    # of wall clock on a slow provider, so the old 120 s would have become the
    # binding limit instead of the round count.
    turn_timeout_s: float = Field(default=300.0, ge=0.05)
    history_token_budget: int = Field(default=8000, ge=1)

    # How long a request to the mod may take before it is treated as failed.
    #
    # This field was referenced by the request path from the start but never
    # declared, which went unnoticed because every call site passed an explicit
    # timeout: the attribute was only read when one was omitted, so the first
    # production request would have raised `AttributeError` on a config object.
    # Fixed here rather than by adding a default at the call site, because a
    # timeout is a setting (SPEC.md 9.2) and belongs with the others.
    request_timeout_s: int = Field(default=30, ge=1)

    # Optional extra object ids to merge over the built-in catalog seed, so the
    # catalog can be extended without editing the package (SPEC.md 6.2.3).
    catalog_path: Annotated[Path | None, BlankAsNone] = None

    # Retries for a *retryable* provider failure, i.e. 429 and 5xx (SPEC.md 6.2.3).
    # Zero disables the retry loop entirely, which is what the tests use so that a
    # failure case does not spend the backoff waiting to fail.
    provider_max_retries: int = Field(default=3, ge=0, le=10)
    provider_retry_base_s: float = Field(default=1.0, ge=0.0, le=30.0)

    # --- Persistence / logging ----------------------------------------
    session_dir: Path = Path("./sessions")
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @model_validator(mode="after")
    def _validate_configuration(self) -> SidecarConfig:
        """Cross-field checks, run once every field is populated.

        This cannot be a field validator: it compares two settings, and a field
        validator only sees values of fields declared before it. Single-field
        bounds (such as the D10 object cap) stay on the ``Field`` itself.
        """
        # Refuse a non-loopback bind unless explicitly opted in (D11, 12).
        if not self.allow_non_loopback and not _is_loopback(self.ws_host):
            raise ValueError(
                f"ws_host {self.ws_host!r} is not a loopback address; "
                "set allow_non_loopback=true to override"
            )
        return self

    # NOTE: there is deliberately no `is_configured` property here.
    #
    # There used to be one, and it was a second copy of the readiness rule that
    # disagreed with the real one: it demanded a `model`, while
    # `providers.create_provider` falls back to a provider default, so a config
    # with a key and no model read as "not configured" while the sidecar happily
    # served turns. Readiness is now decided in exactly one place -- whether a
    # provider can be constructed -- and the handshake asks that instead.
