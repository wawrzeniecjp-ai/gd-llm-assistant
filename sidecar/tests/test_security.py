"""Security-relevant sidecar behaviour (SPEC.md 7.2, 9.2, 12).

These cover the three things the spec treats as trust boundaries: the handshake
token, the loopback-only bind, and secret redaction. They are cheap tests for
expensive mistakes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gd_ai_assistant.auth import load_or_create_token, token_matches
from gd_ai_assistant.config import SidecarConfig
from gd_ai_assistant.redact import REDACTED, redact, redact_value

# --- token file (7.2) -------------------------------------------------


def test_token_is_created_then_stable(tmp_path) -> None:
    path = tmp_path / "token"
    first = load_or_create_token(path)
    assert path.is_file()
    assert len(first) >= 22  # >= 128 bits even at the low end of urlsafe
    assert load_or_create_token(path) == first


def test_empty_token_file_is_regenerated(tmp_path) -> None:
    path = tmp_path / "token"
    path.write_text("   \n", encoding="utf-8")
    token = load_or_create_token(path)
    assert token.strip()
    assert load_or_create_token(path) == token


def test_token_comparison_rejects_wrong_and_non_string() -> None:
    assert token_matches("abc", "abc")
    assert not token_matches("abc", "abd")
    assert not token_matches("abc", None)
    assert not token_matches("abc", 123)


# --- redaction (12) ---------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwxyz012345",
        "sk-ant-abcdefghijklmnopqrstuvwxyz0123",
    ],
)
def test_recognisable_keys_are_scrubbed(secret: str) -> None:
    assert secret not in redact(f"provider said: {secret} failed")
    assert REDACTED in redact(secret)


def test_sensitive_keys_are_masked_at_any_depth() -> None:
    value = {"outer": {"api_key": "hunter2", "nested": [{"authorization": "Bearer x"}]}}
    scrubbed = redact_value(value)
    assert scrubbed["outer"]["api_key"] == REDACTED
    assert scrubbed["outer"]["nested"][0]["authorization"] == REDACTED


# --- loopback enforcement (D11, 12) -----------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_is_accepted(host: str) -> None:
    assert SidecarConfig(ws_host=host).ws_host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.org"])
def test_non_loopback_bind_is_refused(host: str) -> None:
    with pytest.raises(ValidationError, match="not a loopback address"):
        SidecarConfig(ws_host=host)


def test_non_loopback_bind_allowed_only_with_override() -> None:
    config = SidecarConfig(ws_host="0.0.0.0", allow_non_loopback=True)
    assert config.ws_host == "0.0.0.0"


# --- setup state (7.2, 9.2) -------------------------------------------


def test_sidecar_reports_needs_setup_without_credentials() -> None:
    """No key/model means `state: needs_setup`, not a failure to start (A6)."""
    assert SidecarConfig().is_configured is False


def test_sidecar_is_configured_with_provider_key_and_model() -> None:
    config = SidecarConfig(llm_provider="anthropic", api_key="sk-ant-test", model="a-current-model")
    assert config.is_configured is True


def test_objects_per_turn_cannot_exceed_mod_hard_maximum() -> None:
    """D10: the mod's limits are authoritative, so the sidecar cannot exceed them."""
    with pytest.raises(ValidationError, match="less than or equal to 20000"):
        SidecarConfig(max_objects_per_turn=20001)
