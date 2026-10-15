"""A complete turn over the real protocol, through the real server (SPEC.md 13).

`test_tool_loop.py` tests the loop in isolation. This file tests the seam it
cannot: that a `user_message` frame produces the exact event sequence the mod's
panel expects, and that the loop's requests reach the wire as valid protocol
frames and are answered.

The fake mod below is deliberately stricter than a stub. It validates every frame
against `protocol/v1` and enforces the one-open-transaction rule, which is what
turns "the panel looked right" into something a test can check.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from test_handshake import hello_frame
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from gd_ai_assistant import protocol
from gd_ai_assistant.config import SidecarConfig
from gd_ai_assistant.server import SidecarServer


@asynccontextmanager
async def running_sidecar(tmp_path, **overrides) -> AsyncIterator[tuple[SidecarServer, str]]:
    # `scripted_turn_delay_ms=0` keeps the tests fast; the delay exists for the
    # in-editor thinking indicator, not for correctness.
    defaults = {
        "llm_provider": "scripted",
        "scripted_turn_delay_ms": 0,
        # A deliberate failure in a test must not spend the backoff waiting.
        "provider_retry_base_s": 0.0,
    }
    config = SidecarConfig(token_path=tmp_path / "token", **(defaults | overrides))
    sidecar = SidecarServer(config)
    async with serve(
        sidecar.handler, "127.0.0.1", 0, process_request=sidecar.rejects_origin
    ) as server:
        port = server.sockets[0].getsockname()[1]
        yield sidecar, f"ws://127.0.0.1:{port}"


def decode(raw: str) -> dict:
    return json.loads(raw)


async def do_handshake(websocket, token: str) -> dict:
    await websocket.send(hello_frame(token))
    result = decode(await websocket.recv())
    assert result["payload"]["ok"] is True
    history = decode(await websocket.recv())
    assert history["type"] == "history"
    return result["payload"]["result"]


class FakeMod:
    """Answers the sidecar's requests the way the mod does.

    Enforces the two invariants the loop's correctness depends on: at most one
    open transaction, and every frame valid against the schema.
    """

    def __init__(self, websocket, results: dict[str, dict] | None = None) -> None:
        self._websocket = websocket
        self._results = results or {}
        self.received: list[dict] = []
        self.open_txn: str | None = None
        self._txn_counter = 0

    async def serve_until(self, event_type: str, *, limit: int = 25) -> list[dict]:
        """Answer requests until `event_type` arrives. Returns every frame read."""
        frames: list[dict] = []
        for _ in range(limit):
            frame = decode(await asyncio.wait_for(self._websocket.recv(), timeout=5))
            frames.append(frame)

            # Every frame the sidecar sends must be valid: the mod would drop an
            # invalid one with a warning, leaving the turn hung.
            assert protocol.validate_frame(frame) == [], frame

            if frame["id"] is None:
                if frame["type"] == event_type:
                    return frames
                continue

            self.received.append(frame)
            await self._answer(frame)
        raise AssertionError(f"{event_type!r} never arrived; saw {[f['type'] for f in frames]}")

    async def _answer(self, frame: dict) -> None:
        type_name = frame["type"]
        payload = frame["payload"]

        if type_name == "begin_transaction":
            assert self.open_txn is None, "the sidecar opened a second transaction"
            self._txn_counter += 1
            self.open_txn = f"x-{self._txn_counter}"
            await self._reply(frame["id"], {"txn_id": self.open_txn})
            return

        if type_name == "end_transaction":
            assert payload["txn_id"] == self.open_txn, "end for a transaction that is not open"
            self.open_txn = None
            await self._reply(frame["id"], {"txn_id": payload["txn_id"], "summary": {"created": 1}})
            return

        await self._reply(frame["id"], self._results.get(type_name, {}))

    async def _reply(self, request_id: str, result: dict) -> None:
        await self._websocket.send(
            json.dumps(
                {"type": "response", "id": request_id, "payload": {"ok": True, "result": result}}
            )
        )


async def send_user_message(websocket, text: str, message_id: str = "u-1") -> None:
    await websocket.send(
        json.dumps(
            {
                "type": "user_message",
                "id": None,
                "payload": {"message_id": message_id, "text": text},
            }
        )
    )


# -- the event sequence --------------------------------------------------


async def test_a_read_only_turn_produces_the_expected_event_sequence(tmp_path) -> None:
    """SPEC.md 5.2's flow, for a message that asks for no edits."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "how many objects are there?")

        frames = await FakeMod(websocket).serve_until("turn_finished")
        events = [frame for frame in frames if frame["id"] is None]
        types = [frame["type"] for frame in events]

        assert types[0] == "turn_started"
        assert "assistant_message" in types
        assert types[-1] == "turn_finished"

        started = events[0]["payload"]
        finished = events[-1]["payload"]
        assert finished["turn_id"] == started["turn_id"]
        assert finished["status"] == "completed"

        # Every assistant message in the turn addresses the message the panel was
        # told to expect, so it fills in one bubble rather than stacking new ones.
        for event in events:
            if event["type"] == "assistant_message":
                assert event["payload"]["message_id"] == started["message_id"]

        # A read-only turn must not offer Revert (SPEC.md 7.4, 8.9).
        assert finished["txn_id"] is None


async def test_a_mutating_turn_reports_a_transaction_and_actions(tmp_path) -> None:
    """Naming a block type makes the scripted provider plan a real edit."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "add a row of spikes")

        mod = FakeMod(websocket, {"create_objects": {"created": [1, 2, 3], "count": 3}})
        frames = await mod.serve_until("turn_finished")
        finished = frames[-1]["payload"]

        assert finished["status"] == "completed"
        assert finished["txn_id"] is not None
        assert mod.open_txn is None, "the turn left a transaction open"
        assert any(frame["type"] == "create_objects" for frame in mod.received)

        kinds = [action["kind"] for action in finished["actions"]]
        assert kinds == ["create_objects"]
        assert finished["actions"][0]["count"] == 3

        # Usage is reported for runaway-cost visibility (SPEC.md 12).
        assert finished["usage"]["tool_rounds"] >= 1
        assert finished["usage"]["input_tokens"] > 0


async def test_every_frame_the_turn_sends_is_valid(tmp_path) -> None:
    """Implicit in FakeMod, asserted explicitly so a failure names this contract."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "add a row of spikes")

        mod = FakeMod(websocket, {"create_objects": {"created": [1], "count": 1}})
        frames = await mod.serve_until("turn_finished")

        assert frames, "no frames were exchanged"
        for frame in frames:
            assert protocol.validate_frame(frame) == [], frame


# -- the setup state (A6) ------------------------------------------------


async def test_no_provider_reports_needs_setup_and_runs_no_turn(tmp_path) -> None:
    """A6: with nothing configured, the panel shows setup and no turn happens."""
    async with (
        running_sidecar(tmp_path, llm_provider=None, api_key=None, model=None) as (sidecar, url),
        connect(url) as websocket,
    ):
        result = await do_handshake(websocket, sidecar.token)

        assert result["state"] == "needs_setup"
        assert result["detail"], "the panel needs something to show the user"
        assert "scripted" in result["detail"], (
            "the message should say how to run without a provider"
        )

        await send_user_message(websocket, "do something")
        mod = FakeMod(websocket)
        frames = await mod.serve_until("turn_finished")

        # A turn_started/turn_finished pair, so the panel leaves its thinking
        # state, and no editor request at all.
        assert [frame["type"] for frame in frames if frame["id"] is None] == [
            "turn_started",
            "system_message",
            "turn_finished",
        ]
        assert frames[-1]["payload"]["status"] == "failed"
        assert mod.received == [], "a turn ran without a provider configured"


async def test_hello_reports_scripted_as_ready(tmp_path) -> None:
    """The scripted provider needs no key, so it must not report needs_setup."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        result = await do_handshake(websocket, sidecar.token)
        assert result["state"] == "ready"
        assert result["detail"] is None


# -- cancellation and duplicates -----------------------------------------


async def test_cancel_turn_reports_cancelled(tmp_path) -> None:
    # A delay is required: it is the only await point inside the provider, and
    # therefore the only place the cancellation can land.
    async with (
        running_sidecar(tmp_path, scripted_turn_delay_ms=5000) as (sidecar, url),
        connect(url) as websocket,
    ):
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "do something slow")

        started = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert started["type"] == "turn_started"

        await websocket.send(
            json.dumps(
                {
                    "type": "cancel_turn",
                    "id": None,
                    "payload": {"turn_id": started["payload"]["turn_id"]},
                }
            )
        )

        finished = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert protocol.validate_frame(finished) == []
        assert finished["type"] == "turn_finished"
        assert finished["payload"]["status"] == "cancelled"
        assert finished["payload"]["actions"] == []


async def test_a_second_message_during_a_turn_is_refused(tmp_path) -> None:
    """Two concurrent turns would interleave their events in one transcript."""
    async with (
        running_sidecar(tmp_path, scripted_turn_delay_ms=5000) as (sidecar, url),
        connect(url) as websocket,
    ):
        await do_handshake(websocket, sidecar.token)

        await send_user_message(websocket, "first")
        started = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert started["type"] == "turn_started"

        await send_user_message(websocket, "second")

        warning = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert protocol.validate_frame(warning) == []
        assert warning["type"] == "system_message"
        assert warning["payload"]["level"] == "warn"

        await websocket.send(
            json.dumps(
                {
                    "type": "cancel_turn",
                    "id": None,
                    "payload": {"turn_id": started["payload"]["turn_id"]},
                }
            )
        )
        finished = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert finished["payload"]["status"] == "cancelled"


async def test_editor_closed_mid_turn_is_cleaned_up(tmp_path) -> None:
    """D13: closing the editor mid-turn must not leave a task or a transaction."""
    async with (
        running_sidecar(tmp_path, scripted_turn_delay_ms=5000) as (sidecar, url),
        connect(url) as websocket,
    ):
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "slow")
        started = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert started["type"] == "turn_started"

        await websocket.send(json.dumps({"type": "editor_closed", "id": None, "payload": {}}))

        for _ in range(50):
            if sidecar._turn_task is None:  # noqa: SLF001 - asserting internal cleanup
                break
            await asyncio.sleep(0.01)
        assert sidecar._turn_task is None  # noqa: SLF001


# -- awkward input -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["", "x" * 2000, "emoji \N{SPARKLES} and \N{CJK UNIFIED IDEOGRAPH-4E2D}", "<c-FF0000>red</c>"],
)
async def test_awkward_messages_still_produce_a_valid_turn(tmp_path, text: str) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, text)
        frames = await FakeMod(websocket).serve_until("turn_finished")
        assert frames[-1]["payload"]["status"] == "completed"
