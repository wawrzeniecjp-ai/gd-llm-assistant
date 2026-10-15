"""Handshake token handling (SPEC.md 7.2, D11).

Threat: web pages and other local processes can connect to a loopback socket,
because WebSockets are not subject to the same-origin policy. Two defences:

1. The sidecar rejects any upgrade request carrying an `Origin` header
   (browsers always send one; the mod never does).
2. The mod's first message must be `hello` carrying a shared token.

The token is **not** the LLM API key. Accepted residual risk: another process
running as the same OS user can read the token file, which matches the trust
level of the API key file itself.
"""

from __future__ import annotations

import contextlib
import hmac
import secrets
from pathlib import Path

TOKEN_BYTES = 32  # 256 bits, comfortably above the >=128-bit requirement


def load_or_create_token(path: Path) -> str:
    """Read the token file, generating it on first start.

    The file is created with owner-only permissions where the OS supports it.
    """
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token

    token = secrets.token_urlsafe(TOKEN_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    _restrict_permissions(path)
    return token


def _restrict_permissions(path: Path) -> None:
    """Best-effort owner-only permissions. No-op where unsupported (Windows ACLs).

    Windows: chmod only toggles the read-only bit, so this has little effect
    there. The residual risk is accepted and documented above.
    """
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def token_matches(expected: str, presented: object) -> bool:
    """Constant-time comparison, so a timing side channel cannot leak the token."""
    if not isinstance(presented, str):
        return False
    return hmac.compare_digest(expected, presented)
