"""Security-relevant sidecar behaviour (SPEC.md 7.2, 9.2, 12).

These cover the three things the spec treats as trust boundaries: the handshake
token, the loopback-only bind, and secret redaction. They are cheap tests for
expensive mistakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gd_ai_assistant.auth import load_or_create_token, token_matches
from gd_ai_assistant.config import SidecarConfig
from gd_ai_assistant.providers import ProviderUnavailable, create_provider
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

# Readiness is decided by `create_provider`, not by inspecting config fields.
# These assertions used to test a `config.is_configured` property, which was a
# second copy of the same rule and disagreed with this one: it demanded a `model`,
# while the factory falls back to a provider default. Two answers to "is the
# sidecar ready" is one too many, so the property is gone and these test the
# authority the handshake actually consults.


def test_sidecar_reports_needs_setup_without_credentials() -> None:
    """No provider means `state: needs_setup`, not a failure to start (A6)."""
    with pytest.raises(ProviderUnavailable):
        create_provider(SidecarConfig())


def test_blank_env_values_are_treated_as_unset(tmp_path) -> None:
    """`KEY=` in a dotenv file must mean "unset", not "".

    A dotenv file has no way to express "absent" -- `KEY=` is the only way to
    leave a value for the user to fill in -- and pydantic reads that as the empty
    string. Without `BlankAsNone`, the `.env.example` this project ships crashes
    the sidecar on first run with a `literal_error` on `llm_provider` and an
    `int_parsing` error on `turn_token_budget`.

    That is not hypothetical: it is what the launcher did the first time it was
    run for real. This test is the guard.
    """
    env = tmp_path / ".env"
    env.write_text(
        "llm_provider=\napi_key=\nmodel=\nturn_token_budget=\ncatalog_path=\nbase_url=\n",
        encoding="utf-8",
    )

    config = SidecarConfig(_env_file=env)

    assert config.llm_provider is None
    assert config.api_key is None
    assert config.model is None
    assert config.turn_token_budget is None
    assert config.catalog_path is None
    assert config.base_url is None
    # And it still reports needs_setup rather than pretending to be ready.
    with pytest.raises(ProviderUnavailable):
        create_provider(config)


def test_the_shipped_env_example_parses(tmp_path) -> None:
    """The file users are told to copy must actually load.

    Reads the real `.env.example` rather than a copy of its shape, so it fails
    when someone adds a setting to the example without handling blanks.
    """
    example = Path(__file__).resolve().parents[1] / ".env.example"
    assert example.is_file(), "the README tells users to copy this file"

    config = SidecarConfig(_env_file=example)

    assert config.ws_port == 8765
    assert config.max_tool_rounds >= 1


def test_a_provider_is_constructible_with_key_and_model() -> None:
    config = SidecarConfig(llm_provider="anthropic", api_key="sk-ant-test", model="a-current-model")
    assert create_provider(config).name == "anthropic"


def test_the_scripted_provider_needs_no_credentials() -> None:
    """The fake provider must be usable with nothing configured, or it cannot
    serve as the tests' vehicle (SPEC.md 13)."""
    assert create_provider(SidecarConfig(llm_provider="scripted")).name == "scripted"


def test_a_provider_requires_a_key_even_with_a_model() -> None:
    with pytest.raises(ProviderUnavailable, match="api_key"):
        create_provider(SidecarConfig(llm_provider="anthropic", model="a-current-model"))


def test_objects_per_turn_cannot_exceed_mod_hard_maximum() -> None:
    """D10: the mod's limits are authoritative, so the sidecar cannot exceed them."""
    with pytest.raises(ValidationError, match="less than or equal to 20000"):
        SidecarConfig(max_objects_per_turn=20001)
