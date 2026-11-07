"""Shared plumbing for developer scripts that talk to the running game.

The mod connects to the sidecar, not the other way round, so a script that wants to
ask the game something has to *be* the sidecar for a while: it starts the normal
server (same port, same token file, same handshake) with an `on_connect` hook, runs
its routine once the mod is connected, and exits.

Usage, from `sidecar/`, with the game open in the level editor and the normal
`start-sidecar.bat` window **closed** (the two would compete for the port):

    uv run python scripts/<script>.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from gd_ai_assistant.config import SidecarConfig
from gd_ai_assistant.server import SidecarServer

log = logging.getLogger("script")

Routine = Callable[[SidecarServer], Awaitable[Any]]


def run(routine: Routine) -> Any:
    """Serve until a mod connects, run `routine` against it, and return its result."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s",
                        stream=sys.stderr)
    return asyncio.run(_main(routine))


async def _main(routine: Routine) -> Any:
    config = SidecarConfig()
    server = SidecarServer(config)
    done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    async def hook(srv: SidecarServer) -> None:
        if done.done():
            return
        try:
            done.set_result(await routine(srv))
        except Exception as exc:  # reported, not swallowed
            done.set_exception(exc)

    server.on_connect = hook
    serving = asyncio.create_task(server.run(), name="script-server")
    print(
        f"Waiting for the game on ws://{config.ws_host}:{config.ws_port} -- open a level "
        "in the editor (close start-sidecar.bat first).",
        file=sys.stderr,
    )
    try:
        # If the server dies first (typically: the port is taken by another sidecar),
        # say so instead of waiting forever for a connection that cannot come.
        await asyncio.wait({done, serving}, return_when=asyncio.FIRST_COMPLETED)
        if not done.done():
            exc = serving.exception() if not serving.cancelled() else None
            raise SystemExit(
                f"The sidecar server stopped before the game connected: {exc!r}\n"
                f"Is another sidecar (start-sidecar.bat) still running on port "
                f"{config.ws_port}? Close it and try again."
            )
        return done.result()
    finally:
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serving


async def ask(prompt: str) -> str:
    """Wait for the user to press Enter in the console, without blocking the server."""
    return await asyncio.to_thread(input, prompt)


async def ok(server: SidecarServer, type_name: str, payload: dict[str, Any] | None = None,
             timeout: float = 120.0) -> dict[str, Any]:
    """A request that must succeed; raises with the mod's error otherwise."""
    response = await server.request(type_name, payload or {}, timeout=timeout)
    if not response.ok:
        raise RuntimeError(f"{type_name} failed: {response.error}")
    return response.result or {}
