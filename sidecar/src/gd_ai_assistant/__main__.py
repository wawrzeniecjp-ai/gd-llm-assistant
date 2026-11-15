"""Sidecar entry point: `uv run gd-ai-assistant-sidecar` (or `python -m gd_ai_assistant`)."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import SidecarConfig
from .redact import redact

#: The console's lines are also kept here (next to `.env`), so a test run can be read
#: after the window is closed. Rotated at 5 MB, three files kept.
LOG_FILE = Path("logs") / "sidecar.log"


def _configure_logging(level: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    except OSError as exc:  # a read-only folder must not stop the sidecar
        print(f"cannot write {LOG_FILE}: {exc}", file=sys.stderr)
    for handler in handlers:
        handler.setFormatter(fmt)
    logging.basicConfig(level=level.upper(), handlers=handlers)


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
