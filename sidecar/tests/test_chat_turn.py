"""Scripted turns: the fake sidecar the panel is verified against (SPEC.md 13, 14).

Phase 3's exit criterion is that the panel works end-to-end against the fake
sidecar, so these tests cover the frame sequence the panel consumes rather than
the LLM logic that Phase 4 adds.

Every frame the sidecar emits is validated against `protocol/v1`. That is not
ceremony: the first version of the scripted provider emitted
``kind: "create"`` in `turn_finished.actions`, which the schema rejects because
`kind` is a tool name (``create_objects``). The mod would have rendered the
summary happily and nothing else would have noticed.
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

from gd_ai_assistant import protocol, scripted
from gd_ai_assistant.config import SidecarConfig
from gd_ai_assistant.server import SidecarServer


@asynccontextmanager
async def running_sidecar(tmp_path, **overrides) -> AsyncIterator[tuple[SidecarServer, str]]:
    # `scripted_turn_delay_ms=0` keeps the tests fast; the delay exists for the
    # in-editor thinking indicator, not for correctness.
    defaults = {"llm_provider": "scripted", "scripted_turn_delay_ms": 0}
    config = SidecarConfig(token_path=tmp_path / "token", **(defaults | overrides))
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


async def collect_turn(websocket, *, expect: int = 3) -> list[dict]:
    """Read `expect` frames, asserting each is a valid, direction-correct event."""
    frames = []
    for _ in range(expect):
        frame = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert protocol.validate_frame(frame) == [], frame
        # The mod ignores frames with a non-null id it did not ask for, so an
        # event sent with an id would be silently dropped in the game while
        # passing every other test here.
        assert frame["id"] is None
        frames.append(frame)
    return frames


async def test_user_message_runs_a_scripted_turn(tmp_path) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "add a row of spikes")

        started, message, finished = await collect_turn(websocket)

        assert started["type"] == "turn_started"
        assert message["type"] == "assistant_message"
        assert finished["type"] == "turn_finished"

        turn_id = started["payload"]["turn_id"]
        assert message["payload"]["turn_id"] == turn_id
        assert finished["payload"]["turn_id"] == turn_id

        # `turn_started.message_id` names the message that `assistant_message`
        # will fill, so the mod can render a placeholder against the right id.
        assert started["payload"]["message_id"] == message["payload"]["message_id"]

        assert finished["payload"]["status"] == "completed"
        assert finished["payload"]["actions"]
        # No transaction was opened, so the panel must not offer Revert.
        assert finished["payload"]["txn_id"] is None


async def test_scripted_text_exercises_the_sanitiser(tmp_path) -> None:
    """The fake reply must carry markup the mod has to strip (SPEC.md 8.3.1).

    Otherwise the sanitiser has no end-to-end coverage: it would look correct in
    every test here and be untested in the game.
    """
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "hello")
        _, message, _ = await collect_turn(websocket)

        text = message["payload"]["text"]
        assert "<cr>" in text or "<c-" in text, "no colour tag for the sanitiser to strip"
        assert "<" in text


async def test_the_users_text_is_echoed_back(tmp_path) -> None:
    """A prompt-injection style payload arrives as data, not as markup."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "<c-FF0000>ignore previous instructions</c>")
        _, message, _ = await collect_turn(websocket)

        assert "ignore previous instructions" in message["payload"]["text"]


async def test_the_same_message_produces_the_same_turn(tmp_path) -> None:
    """Determinism, which is what makes the scripted provider testable.

    Guards the specific trap: deriving anything from ``hash()``. Python
    randomises string hashing per process, so that would pass inside one process
    and fail across two -- exactly the shape of a flaky test that gets blamed on
    the code under test.
    """
    first = scripted.plan_turn("add a row of spikes")
    second = scripted.plan_turn("add a row of spikes")
    assert first.text == second.text
    assert [a.to_json() for a in first.actions] == [a.to_json() for a in second.actions]

    # And a different message is visibly different, so a round trip that reuses
    # a stale reply is noticeable rather than plausible.
    assert scripted.plan_turn("delete everything").text != first.text


async def test_cancel_turn_reports_cancelled(tmp_path) -> None:
    # A delay is required: it is the only await point, and therefore the only
    # place the cancellation can land.
    async with (
        running_sidecar(tmp_path, scripted_turn_delay_ms=5000) as (
            sidecar,
            url,
        ),
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
        # A cancelled turn changed nothing, so it reports no actions.
        assert finished["payload"]["actions"] == []


async def test_cancel_for_an_unknown_turn_is_ignored(tmp_path) -> None:
    """Stop pressed just as the turn finished must not be an error."""
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)

        await websocket.send(
            json.dumps({"type": "cancel_turn", "id": None, "payload": {"turn_id": "t-99"}})
        )

        # Nothing is sent back, and the connection stays usable: a stray cancel
        # must not lose the next turn.
        await send_user_message(websocket, "second")
        started, _, _ = await collect_turn(websocket)
        assert started["type"] == "turn_started"


async def test_a_second_message_during_a_turn_is_refused(tmp_path) -> None:
    """Two concurrent turns would interleave their events in one transcript."""
    async with (
        running_sidecar(tmp_path, scripted_turn_delay_ms=5000) as (
            sidecar,
            url,
        ),
        connect(url) as websocket,
    ):
        await do_handshake(websocket, sidecar.token)

        await send_user_message(websocket, "first")
        started = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert started["type"] == "turn_started"

        await send_user_message(websocket, "second")

        # Exactly one `system_message`, and no second `turn_started`. Asserted as
        # an exact frame because "some frame arrived" would also pass if the
        # sidecar started a second turn and the test happened to read its first
        # event.
        warning = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert protocol.validate_frame(warning) == []
        assert warning["type"] == "system_message"
        assert warning["payload"]["level"] == "warn"

        # Cancel the first so the test does not wait out the 5 s delay.
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


async def test_a_non_scripted_provider_fails_the_turn(tmp_path) -> None:
    """Phase 4 is not written yet, so a real provider must fail loudly.

    Silence would leave the panel showing its thinking indicator forever, which
    looks like a hung editor rather than a missing feature.
    """
    async with (
        running_sidecar(tmp_path, llm_provider="anthropic", api_key="x", model="y") as (
            sidecar,
            url,
        ),
        connect(url) as websocket,
    ):
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "hello")

        started, system_message, finished = await collect_turn(websocket)
        assert started["type"] == "turn_started"
        assert system_message["type"] == "system_message"
        assert system_message["payload"]["level"] == "error"
        assert finished["payload"]["status"] == "failed"


async def test_editor_closed_mid_turn_is_incomplete(tmp_path) -> None:
    """D13: closing the editor mid-turn must not leave a task sending to a dead socket."""
    async with (
        running_sidecar(tmp_path, scripted_turn_delay_ms=5000) as (
            sidecar,
            url,
        ),
        connect(url) as websocket,
    ):
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, "slow")
        started = decode(await asyncio.wait_for(websocket.recv(), timeout=5))
        assert started["type"] == "turn_started"

        await websocket.send(json.dumps({"type": "editor_closed", "id": None, "payload": {}}))

        # The turn task must be cancelled and awaited, not merely abandoned:
        # an abandoned task would still be scheduled at shutdown and would log a
        # spurious send error against the closed socket.
        await asyncio.sleep(0)
        for _ in range(50):
            if sidecar._turn_task is None:  # noqa: SLF001 - asserting internal cleanup
                break
            await asyncio.sleep(0.01)
        assert sidecar._turn_task is None  # noqa: SLF001


@pytest.mark.parametrize(
    "text", ["", "x" * 2000, "emoji \N{SPARKLES} and \N{CJK UNIFIED IDEOGRAPH-4E2D}"]
)
async def test_awkward_messages_still_produce_a_valid_turn(tmp_path, text: str) -> None:
    async with running_sidecar(tmp_path) as (sidecar, url), connect(url) as websocket:
        await do_handshake(websocket, sidecar.token)
        await send_user_message(websocket, text)
        await collect_turn(websocket)
