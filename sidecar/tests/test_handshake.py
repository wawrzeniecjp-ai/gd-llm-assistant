"""Handshake and connection-lifecycle integration tests (SPEC.md 7.1, 7.2, 7.8).

These start a real sidecar on an ephemeral loopback port and drive it with a
real WebSocket client, which is the Phase 1 exit criterion: a Python server and
a client complete the handshake. They are also the beginning of the "fake mod"
harness from SPEC.md 13.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidStatus

from gd_ai_assistant import protocol
from gd_ai_assistant.config import SidecarConfig
from gd_ai_assistant.server import (
    CLOSE_ANOTHER_CLIENT,
    CLOSE_PROTOCOL_MISMATCH,
    CLOSE_UNAUTHORIZED,
    SidecarServer,
)


@asynccontextmanager
async def running_sidecar(tmp_path, **overrides) -> AsyncIterator[tuple[SidecarServer, str]]:
    """Start a sidecar on an ephemeral port; yield (server, ws_url)."""
    config = SidecarConfig(token_path=tmp_path / "token", **overrides)
    sidecar = SidecarServer(config)
    async with serve(
        sidecar.handler,
        "127.0.0.1",
        0,
        process_request=sidecar.rejects_origin,
    ) as server:
        port = server.sockets[0].getsockname()[1]
        yield sidecar, f"ws://127.0.0.1:{port}"


def hello_frame(token: str, protocol_version: int = protocol.PROTOCOL_VERSION) -> str:
    return json.dumps(
        {
            "type": "hello",
            "id": "m-1",
            "payload": {
                "protocol_version": protocol_version,
                "token": token,
                "client": {
                    "mod_version": "0.1.0",
                    "gd_version": "2.2081",
                    "geode_version": "5.11.1",
                },
                "level": {"key": "My Level", "name": "My Level", "object_count": 1284},
            },
        }
    )


def decode(raw: str) -> dict:
    return json.loads(raw)


async def test_valid_handshake_reports_needs_setup_and_pushes_history(tmp_path) -> None:
    """With no LLM configured the sidecar still completes the handshake (A6)."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await websocket.send(hello_frame(sidecar.token))

        response = decode(await websocket.recv())
        assert response["type"] == "response"
        assert response["id"] == "m-1"
        assert response["payload"]["ok"] is True
        result = response["payload"]["result"]
        assert result["state"] == "needs_setup"
        assert result["protocol_version"] == protocol.PROTOCOL_VERSION
        assert result["detail"]  # a user-facing setup message

        # history is pushed immediately after a successful hello
        history = decode(await websocket.recv())
        assert history["type"] == "history"
        assert history["id"] is None
        assert history["payload"] == {"messages": []}


async def test_handshake_reports_ready_when_configured(tmp_path) -> None:
    async with (
        running_sidecar(
            tmp_path,
            llm_provider="anthropic",
            api_key="sk-ant-not-a-real-key",
            model="a-current-model",
        ) as (sidecar, url),
        connect(url) as websocket,
    ):
        await websocket.send(hello_frame(sidecar.token))
        result = decode(await websocket.recv())["payload"]["result"]
        assert result["state"] == "ready"
        assert result["detail"] is None


async def test_wrong_token_closes_with_4001(tmp_path) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await websocket.send(hello_frame("not-the-right-token"))
        with pytest.raises(ConnectionClosedError) as excinfo:
            await websocket.recv()
        assert excinfo.value.rcvd.code == CLOSE_UNAUTHORIZED


async def test_first_frame_must_be_hello(tmp_path) -> None:
    """Anything other than hello as the first frame is 4001 (7.2)."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await websocket.send(json.dumps({"type": "ping", "id": "m-1", "payload": {}}))
        with pytest.raises(ConnectionClosedError) as excinfo:
            await websocket.recv()
        assert excinfo.value.rcvd.code == CLOSE_UNAUTHORIZED


async def test_protocol_version_mismatch_is_rejected(tmp_path) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await websocket.send(hello_frame(sidecar.token, protocol_version=99))

        response = decode(await websocket.recv())
        assert response["payload"]["ok"] is False
        assert response["payload"]["error"]["code"] == "INVALID_REQUEST"

        with pytest.raises(ConnectionClosedError) as excinfo:
            await websocket.recv()
        assert excinfo.value.rcvd.code == CLOSE_PROTOCOL_MISMATCH


async def test_origin_header_is_refused(tmp_path) -> None:
    """Browsers always send Origin; the mod never does (D11)."""
    async with running_sidecar(tmp_path) as (sidecar, url):
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(url, additional_headers={"Origin": "http://evil.example"}):
                pass
        assert excinfo.value.response.status_code == 403


async def test_second_client_is_refused_with_4003(tmp_path) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as first:
        await first.send(hello_frame(sidecar.token))
        assert decode(await first.recv())["payload"]["ok"] is True

        async with connect(url) as second:
            with pytest.raises(ConnectionClosedError) as excinfo:
                await second.recv()
            assert excinfo.value.rcvd.code == CLOSE_ANOTHER_CLIENT


async def test_ping_returns_pong(tmp_path) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await websocket.send(hello_frame(sidecar.token))
        await websocket.recv()
        await websocket.recv()

        await websocket.send(json.dumps({"type": "ping", "id": "m-9", "payload": {}}))
        response = decode(await websocket.recv())
        assert response["id"] == "m-9"
        assert response["payload"] == {"ok": True, "result": {"pong": True}}


async def test_editor_closed_closes_the_socket(tmp_path) -> None:
    """The mod sends editor_closed, then closes with 1000 (7.6, 7.8)."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await websocket.send(hello_frame(sidecar.token))
        await websocket.recv()
        await websocket.recv()

        await websocket.send(json.dumps({"type": "editor_closed", "id": None, "payload": {}}))
        # A clean 1000 close surfaces as ConnectionClosedOK, so catch the base
        # class here rather than ConnectionClosedError.
        with pytest.raises(ConnectionClosed) as excinfo:
            await websocket.recv()
        assert excinfo.value.rcvd.code == 1000


async def test_malformed_json_does_not_close_the_connection(tmp_path) -> None:
    """A bad frame is dropped; the session survives (fuzz expectation, 13)."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await websocket.send(hello_frame(sidecar.token))
        await websocket.recv()
        await websocket.recv()

        await websocket.send("{not json at all")
        await websocket.send(json.dumps({"type": "ping", "id": "m-2", "payload": {}}))

        response = decode(await websocket.recv())
        assert response["id"] == "m-2"
        assert response["payload"]["ok"] is True


async def test_unknown_type_gets_a_structured_error(tmp_path) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await websocket.send(hello_frame(sidecar.token))
        await websocket.recv()
        await websocket.recv()

        await websocket.send(json.dumps({"type": "nonsense", "id": "m-3", "payload": {}}))
        response = decode(await websocket.recv())
        assert response["id"] == "m-3"
        assert response["payload"]["ok"] is False
        assert response["payload"]["error"]["code"] == "INVALID_REQUEST"


async def test_hello_records_the_game_version_and_flags_stale_data(tmp_path, caplog) -> None:
    """dump_roles stamps the role table with this version, and an upgrade is logged."""
    import logging

    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        with caplog.at_level(logging.WARNING):
            await websocket.send(hello_frame(sidecar.token))
            await websocket.recv()
        assert sidecar.gd_version == "2.2081"
    # The committed data was measured on a newer version than this hello claims.
    assert any("dump_roles.py" in r.getMessage() for r in caplog.records)
