"""WebSocket server (SPEC.md 7.1, 7.2, 7.3, 7.8).

Implements the connection lifecycle:

1. Listen on a loopback address; refuse non-loopback unless opted in.
2. Reject any upgrade carrying an `Origin` header (browsers always send one).
3. Allow exactly one authenticated client; close extras with 4003.
4. Require `hello` with a valid token as the first frame (4001 otherwise).
5. Reply with the `hello` result and push `history`.
6. Issue requests to the mod and correlate replies by `id` (7.3), with a
   per-request timeout.

The LLM tool loop, transactions and the editor request set beyond `ping` /
`get_editor_state` are Phase 4 work and are deliberately absent here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request
from websockets.http11 import Response as HttpResponse

from . import __version__, protocol
from .auth import load_or_create_token, token_matches
from .config import SidecarConfig

log = logging.getLogger("sidecar")

# WebSocket close codes (SPEC.md 7.1, 7.2). The 4000-4999 range is reserved for
# application use by RFC 6455.
CLOSE_UNAUTHORIZED = 4001
CLOSE_PROTOCOL_MISMATCH = 4002
CLOSE_ANOTHER_CLIENT = 4003


@dataclass(frozen=True)
class Response:
    """A correlated reply from the mod (SPEC.md 7.3)."""

    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @property
    def code(self) -> str | None:
        """The error code, when the response failed."""
        return None if self.error is None else self.error.get("code")


class SidecarServer:
    """Single-client, authenticated protocol endpoint."""

    def __init__(self, config: SidecarConfig) -> None:
        self.config = config
        self.token = load_or_create_token(config.token_path)
        self._client: ServerConnection | None = None
        self._client_lock = asyncio.Lock()
        # Outstanding requests keyed by correlation id. The two senders use
        # disjoint prefixes (m- / s-) so an id is unambiguous in logs (7.3).
        self._pending: dict[str, asyncio.Future[Response]] = {}
        self._next_request_id = 0

    # -- connection lifecycle -----------------------------------------

    async def rejects_origin(
        self, connection: ServerConnection, request: Request
    ) -> HttpResponse | None:
        """Refuse browser-originated upgrades.

        The mod does not send an `Origin` header; browsers always do. Rejecting
        its mere presence closes the whole class of local web-page attacks (D11).

        Returns None to accept the handshake, or a Response to reject it. Note
        this is the asyncio-server contract (`connection.respond`), not the
        legacy tuple return.
        """
        if request.headers.get("Origin") is not None:
            log.warning("rejected upgrade carrying an Origin header")
            return connection.respond(HTTPStatus.FORBIDDEN, "Origin header not allowed\n")
        return None

    async def handler(self, websocket: ServerConnection) -> None:
        async with self._client_lock:
            if self._client is not None:
                log.warning("refusing a second client (4003)")
                await websocket.close(CLOSE_ANOTHER_CLIENT, "another client is connected")
                return
            self._client = websocket

        try:
            await self._serve_client(websocket)
        except ConnectionClosed:
            log.info("client disconnected")
        finally:
            async with self._client_lock:
                if self._client is websocket:
                    self._client = None

    async def _serve_client(self, websocket: ServerConnection) -> None:
        level_key = await self._handshake(websocket)
        if level_key is None:
            return

        log.info("client ready for level %r", level_key)
        await self._send_event(websocket, "history", {"messages": []})

        # The read loop must already be running before we issue any request:
        # otherwise the reply would sit in the socket with nothing to dispatch
        # it, and `request()` would deadlock until its timeout.
        reader = asyncio.create_task(self._read_loop(websocket), name="sidecar-read-loop")
        try:
            if self.config.probe_on_connect:
                await self._probe(websocket)
            await reader
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            # Never leave a caller awaiting a reply that can no longer arrive.
            self._fail_pending("client disconnected")

    async def _read_loop(self, websocket: ServerConnection) -> None:
        async for raw in websocket:
            frame = self._decode(raw)
            if frame is None:
                continue
            await self._dispatch(websocket, frame)

    def _fail_pending(self, reason: str) -> None:
        """Resolve every outstanding request with a failure, and forget them."""
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(
                    Response(ok=False, error={"code": "CANCELLED", "message": reason})
                )
            self._pending.pop(request_id, None)

    # -- requesting the mod (SPEC.md 7.3, 7.5) -------------------------

    async def request(
        self,
        type_name: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Response:
        """Send a request to the mod and await its correlated response.

        Never raises for a protocol-level failure: a timeout or a disconnect is
        reported as `ok=False` with the appropriate error code, so callers can
        tell the user what happened (SPEC.md 11).
        """
        websocket = self._client
        if websocket is None:
            return Response(
                ok=False,
                error={"code": "EDITOR_NOT_OPEN", "message": "no client is connected"},
            )

        self._next_request_id += 1
        request_id = f"s-{self._next_request_id}"
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        frame = {"type": type_name, "id": request_id, "payload": payload or {}}
        try:
            await websocket.send(json.dumps(frame))
            return await asyncio.wait_for(
                future, timeout if timeout is not None else self.config.request_timeout_s
            )
        except TimeoutError:
            log.warning("request %s (%s) timed out", request_id, type_name)
            return Response(
                ok=False,
                error={
                    "code": "INTERNAL_ERROR",
                    "message": f"request {type_name} timed out",
                },
            )
        finally:
            self._pending.pop(request_id, None)

    def _resolve(self, frame: dict[str, Any]) -> None:
        """Complete the pending request this response belongs to."""
        request_id = frame.get("id")
        if not isinstance(request_id, str) or request_id not in self._pending:
            log.warning("response for unknown or missing request id %r", request_id)
            return

        payload = frame.get("payload") or {}
        response = Response(
            ok=bool(payload.get("ok")),
            result=payload.get("result"),
            error=payload.get("error"),
        )

        future = self._pending.pop(request_id)
        if not future.done():
            future.set_result(response)

    async def _probe(self, websocket: ServerConnection) -> None:
        """Phase 1 smoke check: exercise `ping` and `get_editor_state`.

        Enabled by `probe_on_connect`. Verifies the request/response path end to
        end and logs what the mod reports, which is the Phase 1 exit criterion.
        """
        pong = await self.request("ping", {}, timeout=5)
        if not pong.ok or pong.result != {"pong": True}:
            log.error("probe: ping failed: %s", pong.error)
            return
        log.info("probe: ping -> pong")

        state = await self.request("get_editor_state", {}, timeout=5)
        if not state.ok:
            # Expected outside the editor, and informative either way.
            log.info("probe: get_editor_state -> %s", state.code)
            return
        log.info("probe: get_editor_state -> %s", json.dumps(state.result))

    # -- handshake -----------------------------------------------------

    async def _handshake(self, websocket: ServerConnection) -> str | None:
        """Validate the first frame. Returns the level key, or None on rejection."""
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        except (TimeoutError, ConnectionClosed):
            return None

        frame = self._decode(raw, quiet=True)
        if frame is None or frame.get("type") != "hello":
            await websocket.close(CLOSE_UNAUTHORIZED, "first frame must be hello")
            return None

        payload = frame.get("payload") or {}

        if not token_matches(self.token, payload.get("token")):
            log.warning("rejected connection: token mismatch")
            await websocket.close(CLOSE_UNAUTHORIZED, "token mismatch")
            return None

        if payload.get("protocol_version") != protocol.PROTOCOL_VERSION:
            log.warning(
                "protocol version mismatch: client sent %r", payload.get("protocol_version")
            )
            await self._send_response(
                websocket,
                frame.get("id"),
                ok=False,
                error={
                    "code": "INVALID_REQUEST",
                    "message": (
                        f"protocol_version {payload.get('protocol_version')!r} is not "
                        f"supported; this sidecar speaks {protocol.PROTOCOL_VERSION}"
                    ),
                    "details": {},
                },
            )
            await websocket.close(CLOSE_PROTOCOL_MISMATCH, "protocol version mismatch")
            return None

        state = "ready" if self.config.is_configured else "needs_setup"
        detail = (
            None
            if self.config.is_configured
            else "No API key configured. Set it in the sidecar config and restart."
        )

        await self._send_response(
            websocket,
            frame.get("id"),
            ok=True,
            result={
                "protocol_version": protocol.PROTOCOL_VERSION,
                "sidecar_version": __version__,
                "state": state,
                "detail": detail,
                "history_count": 0,
            },
        )
        client = payload.get("client") or {}
        level = payload.get("level") or {}
        log.info(
            "hello from mod %s (GD %s, Geode %s), level %r, %s objects",
            client.get("mod_version"),
            client.get("gd_version"),
            client.get("geode_version"),
            level.get("name"),
            level.get("object_count"),
        )
        return str(level.get("key", ""))

    # -- dispatch ------------------------------------------------------

    async def _dispatch(self, websocket: ServerConnection, frame: dict) -> None:
        # A response answers a request; it is never itself a request, so it must
        # never draw a reply -- not even an error reply for a malformed one.
        # Replying to a reply invites an unbounded echo loop between the two
        # sides, which is exactly what an early build of the mod triggered.
        # Resolve first, and return unconditionally.
        if frame.get("type") == "response":
            self._resolve(frame)
            return

        problems = protocol.validate_envelope(frame)
        if problems:
            log.warning("invalid envelope: %s", "; ".join(problems))
            await self._send_response(
                websocket,
                frame.get("id"),
                ok=False,
                error={"code": "INVALID_REQUEST", "message": "malformed frame", "details": {}},
            )
            return

        type_name = frame["type"]

        spec = protocol.spec_for(type_name)
        if spec is None:
            log.warning("unknown message type %r", type_name)
            await self._send_response(
                websocket,
                frame.get("id"),
                ok=False,
                error={
                    "code": "INVALID_REQUEST",
                    "message": f"unknown message type {type_name!r}",
                    "details": {},
                },
            )
            return

        if type_name == "ping":
            await self._send_response(websocket, frame.get("id"), ok=True, result={"pong": True})
            return

        if type_name == "editor_closed":
            log.info("editor closed; closing socket with 1000")
            await websocket.close(1000, "editor closed")
            return

        # Everything else is Phase 4 work (tool loop, requests to the mod).
        log.info("unhandled message type %r (not implemented yet)", type_name)

    # -- helpers -------------------------------------------------------

    def _decode(self, raw: str | bytes, quiet: bool = False) -> dict | None:
        limit = 1_048_576  # 1 MB default inbound frame limit (SPEC.md 6.1.4)
        if isinstance(raw, bytes):
            if len(raw) > limit:
                log.warning("dropping oversized frame (%d bytes)", len(raw))
                return None
            raw = raw.decode("utf-8", errors="replace")
        elif len(raw) > limit:
            log.warning("dropping oversized frame (%d chars)", len(raw))
            return None

        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            if not quiet:
                log.warning("dropping non-JSON frame: %s", exc)
            return None

        if not isinstance(frame, dict):
            log.warning("dropping non-object frame")
            return None
        return frame

    async def _send_response(
        self, websocket: ServerConnection, request_id: object, *, ok: bool, **payload: object
    ) -> None:
        frame = {
            "type": "response",
            "id": request_id if isinstance(request_id, str) else None,
            "payload": {"ok": ok, **payload},
        }
        await websocket.send(json.dumps(frame))

    async def _send_event(self, websocket: ServerConnection, type_name: str, payload: dict) -> None:
        await websocket.send(json.dumps({"type": type_name, "id": None, "payload": payload}))

    # -- entry point ---------------------------------------------------

    async def run(self) -> None:
        log.info(
            "listening on ws://%s:%d (protocol v%d)",
            self.config.ws_host,
            self.config.ws_port,
            protocol.PROTOCOL_VERSION,
        )
        if not self.config.is_configured:
            log.warning("no LLM configured; the panel will show the needs_setup state")

        async with serve(
            self.handler,
            self.config.ws_host,
            self.config.ws_port,
            process_request=self.rejects_origin,
        ):
            await asyncio.Future()  # run until cancelled
