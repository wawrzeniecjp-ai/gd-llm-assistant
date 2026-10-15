"""Request/response path and the Phase 1 probe (SPEC.md 7.3, 7.5, 14).

The sidecar issues requests to the mod and correlates replies by `id`. These
tests play the mod's side with a real client, covering the happy path, a
timeout, and a disconnect mid-request.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

# `tests/` has no __init__.py, so pytest puts this directory on sys.path and
# the shared helper is importable by name.
from test_handshake import hello_frame
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from gd_ai_assistant import protocol
from gd_ai_assistant.config import SidecarConfig
from gd_ai_assistant.server import SidecarServer


@asynccontextmanager
async def running_sidecar(tmp_path, **overrides) -> AsyncIterator[tuple[SidecarServer, str]]:
    config = SidecarConfig(token_path=tmp_path / "token", **overrides)
    sidecar = SidecarServer(config)
    async with serve(
        sidecar.handler, "127.0.0.1", 0, process_request=sidecar.rejects_origin
    ) as server:
        port = server.sockets[0].getsockname()[1]
        yield sidecar, f"ws://127.0.0.1:{port}"


def decode(raw: str) -> dict:
    return json.loads(raw)


async def do_handshake(websocket, token: str) -> None:
    await websocket.send(hello_frame(token))
    assert decode(await websocket.recv())["payload"]["ok"] is True
    assert decode(await websocket.recv())["type"] == "history"


async def answer_next_request(websocket, result: dict) -> dict:
    """Read one request frame and reply to it. Returns the request frame."""
    request = decode(await websocket.recv())
    assert request["type"] != "response"
    await websocket.send(
        json.dumps(
            {"type": "response", "id": request["id"], "payload": {"ok": True, "result": result}}
        )
    )
    return request


async def test_request_is_correlated_and_answered(tmp_path) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)

        async def mod_side() -> dict:
            return await answer_next_request(websocket, {"pong": True})

        responder = asyncio.create_task(mod_side())
        response = await sidecar.request("ping", {}, timeout=5)
        request = await responder

        assert response.ok is True
        assert response.result == {"pong": True}

        # The sidecar's ids use the s- prefix, so they never collide with
        # the mod's m- ids in logs (7.3).
        assert request["id"].startswith("s-")
        assert request["payload"] == {}
        # And the request the sidecar sent matches the schema for its type.
        assert protocol.validate_frame(request) == []


async def test_request_echoes_a_get_editor_state_result(tmp_path) -> None:
    """The Phase 1 exit criterion: an editor state query round trips (7.5)."""
    result = {
        "object_count": 1284,
        "grid_size": 30,
        "playtesting": False,
        "editor_layer": 1,
    }
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)

        responder = asyncio.create_task(answer_next_request(websocket, result))
        response = await sidecar.request("get_editor_state", {}, timeout=5)
        request = await responder

        assert response.ok is True
        assert response.result == result
        assert protocol.validate_frame(request) == []


async def test_error_response_is_surfaced_not_raised(tmp_path) -> None:
    """A mod-side failure comes back as ok=False, so callers can explain it (11)."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)

        async def mod_side() -> None:
            request = decode(await websocket.recv())
            await websocket.send(
                json.dumps(
                    {
                        "type": "response",
                        "id": request["id"],
                        "payload": {
                            "ok": False,
                            "error": {
                                "code": "EDITOR_NOT_OPEN",
                                "message": "No editor layer is active.",
                            },
                        },
                    }
                )
            )

        responder = asyncio.create_task(mod_side())
        response = await sidecar.request("get_editor_state", {}, timeout=5)
        await responder

        assert response.ok is False
        assert response.code == "EDITOR_NOT_OPEN"
        assert response.result is None


async def test_request_times_out_when_the_mod_never_replies(tmp_path) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)
        response = await sidecar.request("ping", {}, timeout=0.2)

        assert response.ok is False
        assert response.code == "INTERNAL_ERROR"


async def test_request_without_a_client_fails_fast(tmp_path) -> None:
    async with running_sidecar(tmp_path) as (sidecar, _url):
        response = await sidecar.request("ping", {}, timeout=1)
        assert response.ok is False
        assert response.code == "EDITOR_NOT_OPEN"


async def test_disconnect_fails_a_pending_request(tmp_path) -> None:
    """A dropped client must not strand the awaiting caller (7.8 step 7)."""
    async with running_sidecar(tmp_path) as (sidecar, url):
        async with connect(url) as websocket:
            await do_handshake(websocket, sidecar.token)

            pending = asyncio.create_task(sidecar.request("ping", {}, timeout=5))
            # Read and discard the request frame, then drop the connection.
            await websocket.recv()

        response = await pending
        assert response.ok is False
        assert response.code == "CANCELLED"


async def test_probe_pings_and_queries_editor_state(tmp_path) -> None:
    """`probe_on_connect` exercises both request types on a live connection."""
    async with (
        running_sidecar(tmp_path, probe_on_connect=True) as (sidecar, url),
        connect(url) as websocket,
    ):
        await do_handshake(websocket, sidecar.token)

        seen = []
        first = await answer_next_request(websocket, {"pong": True})
        seen.append(first["type"])
        second = await answer_next_request(
            websocket, {"object_count": 0, "grid_size": 30, "playtesting": True}
        )
        seen.append(second["type"])

        assert seen == ["ping", "get_editor_state"]

        # Let the server's probe task observe both replies before teardown.
        await asyncio.sleep(0)


async def test_unknown_response_id_is_ignored(tmp_path) -> None:
    """A stray response must not crash the read loop (fuzz expectation, 13)."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)

        await websocket.send(
            json.dumps({"type": "response", "id": "s-999", "payload": {"ok": True, "result": {}}})
        )

        # The session survives, so a follow-up request still works.
        responder = asyncio.create_task(answer_next_request(websocket, {"pong": True}))
        response = await sidecar.request("ping", {}, timeout=5)
        await responder
        assert response.ok is True


async def test_malformed_response_does_not_draw_a_reply(tmp_path) -> None:
    """A response must never draw a reply, not even an error one.

    Replying to a reply invites an unbounded echo loop: an early mod build sent
    an invalid `response`, the sidecar answered it with an error `response`, and
    the two ping-ponged until the connection was killed. Regression guard.
    """
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)

        # Deliberately invalid: `id` must be a string or null (7.3).
        await websocket.send(
            json.dumps({"type": "response", "id": {}, "payload": {"ok": True, "result": {}}})
        )

        # If the sidecar replied, this read would return that frame instead
        # of staying pending. A ping is used to prove which it was.
        responder = asyncio.create_task(answer_next_request(websocket, {"pong": True}))
        response = await sidecar.request("ping", {}, timeout=5)
        request = await responder

        assert response.ok is True
        assert request["type"] == "ping"


@pytest.mark.parametrize("bad_id", [None, 42])
async def test_response_with_unusable_id_is_ignored(tmp_path, bad_id) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)

        await websocket.send(
            json.dumps({"type": "response", "id": bad_id, "payload": {"ok": True, "result": {}}})
        )

        responder = asyncio.create_task(answer_next_request(websocket, {"pong": True}))
        response = await sidecar.request("ping", {}, timeout=5)
        await responder
        assert response.ok is True
