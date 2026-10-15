"""Secret redaction for logs and user-facing errors (SPEC.md 12).

The API key must never appear in a log line, an exception traceback, or a
`system_message` sent to the mod. Every provider-facing string passes through
:func:`redact` before it is logged or serialised.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

# Provider key shapes we can recognise without knowing the configured value.
_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI and compatible
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),  # Anthropic
)

_SENSITIVE_KEYS = frozenset(
    {"api_key", "api-key", "apikey", "authorization", "token", "password", "secret"}
)


def redact(text: str) -> str:
    """Scrub recognisable secrets from an arbitrary string."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively scrub a JSON-like structure, masking sensitive keys."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if key.lower() in _SENSITIVE_KEYS else redact_value(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value
