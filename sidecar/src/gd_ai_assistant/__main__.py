"""Sidecar entry point: `uv run gd-ai-assistant-sidecar` (or `python -m gd_ai_assistant`)."""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import SidecarConfig
from .redact import redact


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main() -> int:
    config = SidecarConfig()
    _configure_logging(config.log_level)
    log = logging.getLogger("sidecar")

    if config.llm_provider:
        log.info("provider: %s, model: %s", config.llm_provider, config.model)
        log.info("api key: %s", redact(config.api_key or "(unset)"))

    from .server import SidecarServer

    server = SidecarServer(config)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        log.info("shutting down")
    except OSError as exc:
        # Port already in use, or a non-loopback bind without the override.
        log.error("cannot start: %s", redact(str(exc)))
        log.error("the port is configurable via ws_port; check for another GD instance")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
