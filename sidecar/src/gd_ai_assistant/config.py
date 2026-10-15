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
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class SidecarConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM provider -------------------------------------------------
    llm_provider: Literal["anthropic", "openai", "openai_compatible"] | None = None
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)

    # --- Transport ----------------------------------------------------
    # `allow_non_loopback` must precede `ws_host` only if the check were a
    # field validator; it is a model validator instead, so order is free.
    ws_host: str = "127.0.0.1"
    ws_port: int = Field(default=8765, ge=1, le=65535)
    allow_non_loopback: bool = False
    token_path: Path = Field(default_factory=_default_token_path)

    # --- Tool loop limits (SPEC.md 6.2.3, 10.2) -----------------------
    max_tool_rounds: int = Field(default=10, ge=1, le=100)
    # Bounded by the mod's hard maximum: the mod's caps are authoritative (D10).
    max_objects_per_turn: int = Field(default=5000, ge=1, le=20000)
    turn_token_budget: int | None = None
    turn_timeout_s: int = Field(default=120, ge=1)
    history_token_budget: int = Field(default=8000, ge=1)

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

    @property
    def is_configured(self) -> bool:
        """False makes the sidecar report `state: needs_setup` after `hello` (SPEC.md 7.2)."""
        return bool(self.api_key and self.model and self.llm_provider)
