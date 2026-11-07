"""WebSocket server (SPEC.md 7.1, 7.2, 7.3, 7.8).

Implements the connection lifecycle:

1. Listen on a loopback address; refuse non-loopback unless opted in.
2. Reject any upgrade carrying an `Origin` header (browsers always send one).
3. Allow exactly one authenticated client; close extras with 4003.
4. Require `hello` with a valid token as the first frame (4001 otherwise).
5. Reply with the `hello` result and push `history`.
6. Issue requests to the mod and correlate replies by `id` (7.3), with a
   per-request timeout.
7. Run the LLM tool loop for each `user_message` (SPEC.md 5.2, 6.2.3).

The turn itself lives in `agent.ToolLoop`; this module owns the transport and the
event sequence around it. That split is what lets the loop be tested against a
fake editor link and a scripted provider, with no WebSocket involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request
from websockets.http11 import Response as HttpResponse

from . import __version__, agent, catalog, protocol
from .auth import load_or_create_token, token_matches
from .config import SidecarConfig
from .design.versions import data_version_warnings
from .providers import Provider, ProviderUnavailable, create_provider

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

        # Request types the connected mod answered with UNSUPPORTED_REQUEST. The tool
        # loop reads and extends this set; it is reset on every new connection.
        self.unsupported_requests: set[str] = set()

        # The connected game's version, from the hello. The data files dumped from the
        # game (roles.json, physics_constants.json) record the version they came from;
        # a mismatch is logged so an upgrade is noticed (docs/UPGRADING.md).
        self.gd_version: str | None = None

        # Developer scripts (`scripts/dump_roles.py`, `scripts/check_foundations.py`)
        # run their requests from here once a mod has connected and the handshake is
        # done. None in normal operation.
        self.on_connect: Callable[[SidecarServer], Awaitable[None]] | None = None
        self._hook_task: asyncio.Task[None] | None = None

        # The turn currently running, if any. The protocol has no request to
        # await here: `user_message` is an event, and the answer arrives as a
        # sequence of events, so the turn owns a task rather than a future.
        self._turn_task: asyncio.Task[None] | None = None
        self._turn_id: str | None = None
        self._next_turn = 0

        # Built once at construction rather than per turn, so a configuration
        # problem is reported at startup instead of on the user's first message.
        # A missing provider is not fatal -- the handshake reports `needs_setup`
        # and the panel explains it (SPEC.md 8.8, A6).
        self._provider: Provider | None = None
        self._setup_detail: str | None = None
        try:
            self._provider = create_provider(config)
        except ProviderUnavailable as exc:
            self._setup_detail = str(exc)
            log.warning("no provider configured: %s", exc)

        self._catalog = catalog.load_catalog(
            str(config.catalog_path) if config.catalog_path else None
        )
        # One history per level session, replaced when the level key changes.
        self._history = agent.History(config.history_token_budget)
        self._history_key: str | None = None

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
            # A new connection may be a different mod build, so whatever the last one
            # could not do is forgotten (LEVEL-DESIGN-SPEC.md LD14).
            self.unsupported_requests = set()

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

        # A new level means a new session, and SPEC.md 6.2.5 says panel state
        # resets with it. Conversation history is part of that: carrying it across
        # levels would have the model discussing a level the user is no longer in.
        if self._history_key != level_key:
            self._history.clear()
            self._history_key = level_key

        history_messages = self._history.messages()
        await self._send_event(
            websocket,
            "history",
            {
                "messages": [
                    # Only user and assistant text is kept (see `agent.History`),
                    # so this is the whole transcript as far as the panel is
                    # concerned. `ts` is required by the schema; the store does not
                    # date messages yet, so it is zero rather than a plausible lie.
                    {"role": message.role, "text": message.text, "ts": 0}
                    for message in history_messages
                ]
            },
        )

        # The read loop must already be running before we issue any request:
        # otherwise the reply would sit in the socket with nothing to dispatch
        # it, and `request()` would deadlock until its timeout.
        reader = asyncio.create_task(self._read_loop(websocket), name="sidecar-read-loop")
        try:
            if self.config.probe_on_connect:
                await self._probe(websocket)
            if self.on_connect is not None:
                # Run beside the read loop, not before it: the hook issues requests,
                # and their replies arrive through `reader`.
                self._hook_task = asyncio.create_task(self.on_connect(self), name="on-connect")
            await reader
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            # A turn outlives the read loop by design, so it is not stopped by
            # `reader` finishing. Left running, it would try to send `turn_started`
            # or `turn_finished` on a closed socket and log a spurious error for
            # what is really just "the editor closed mid-turn" (D13: the turn is
            # `incomplete`).
            await self._cancel_running_turn(disconnected=True)
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

        # The object-type inventory matters more than the counts above: it is the
        # only trustworthy source of object ids, so a break in it silently makes
        # the assistant unable to create anything. Cheap to check here.
        types = await self.request("list_object_types", {}, timeout=10)
        if not types.ok:
            log.error("probe: list_object_types failed: %s", types.error)
            return
        log.info("probe: list_object_types -> %s", json.dumps(types.result))

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

        state = "ready" if self._provider is not None else "needs_setup"
        detail = self._setup_detail

        await self._send_response(
            websocket,
            frame.get("id"),
            ok=True,
            result={
                "protocol_version": protocol.PROTOCOL_VERSION,
                "sidecar_version": __version__,
                "state": state,
                "detail": detail,
                "history_count": len(self._history),
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
        gd_version = client.get("gd_version")
        self.gd_version = gd_version if isinstance(gd_version, str) else None
        if self.gd_version:
            for warning in data_version_warnings(self.gd_version):
                log.warning("%s", warning)
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

        if type_name == "user_message":
            await self._handle_user_message(websocket, frame["payload"])
            return

        if type_name == "cancel_turn":
            await self._handle_cancel_turn(websocket, frame["payload"])
            return

        if type_name == "transaction_reverted":
            # The user undid a turn with the panel's Revert action. The transcript stays,
            # but the model must not keep believing those edits are still in place --
            # and "something changed" is too weak a hint here, since the model's own
            # transcript says the work succeeded. See `agent.REVERTED_NOTE`.
            log.info("transaction reverted by the mod: %r", frame["payload"].get("txn_id"))
            self._history.mark_level_changed(agent.REVERTED_NOTE)
            return

        if type_name == "level_changed":
            # The level was edited by hand. Marked rather than acted on: the loop
            # tells the model at the start of the next turn, because sending a
            # message mid-turn would interleave with the turn's own events.
            payload = frame["payload"]
            log.info(
                "level changed by hand: +%s -%s ~%s",
                payload.get("added", 0),
                payload.get("removed", 0),
                payload.get("modified", 0),
            )
            self._history.mark_level_changed()
            return

        log.info("unhandled message type %r", type_name)

    # -- turns ---------------------------------------------------------

    async def _handle_user_message(self, websocket: ServerConnection, payload: dict) -> None:
        """Start a turn for a `user_message` event (SPEC.md 7.4).

        No response frame is sent. The event carries a null id, so there is
        nothing to correlate: the turn reports itself through `turn_started`,
        `assistant_message` and `turn_finished`.
        """
        text = payload.get("text", "")
        log.info("user_message (%d chars)", len(text))

        if self._turn_task is not None and not self._turn_task.done():
            # The mod disables its input while a turn runs (SPEC.md 8.5), so this
            # is a duplicated or stale frame rather than a user double-send.
            # Ignoring it is the safe reading of the protocol, which defines no
            # error code for it: starting a second turn would interleave two
            # turns' events, and cancelling the first would silently discard work
            # the user did not ask to abandon.
            log.warning("ignoring user_message; turn %r is still running", self._turn_id)
            await self._send_event(
                websocket,
                "system_message",
                {"level": "warn", "text": "A turn is already running."},
            )
            return

        if self._provider is None:
            # Reaching here means the handshake already reported `needs_setup`, so
            # the panel is showing the setup message and the input is disabled. A
            # turn that slipped through anyway gets an explicit refusal rather
            # than silence, and no request reaches a provider (A6).
            turn_id = self._allocate_turn_id()
            await self._send_event(
                websocket, "turn_started", {"turn_id": turn_id, "message_id": f"a-{turn_id}"}
            )
            await self._send_event(
                websocket,
                "system_message",
                {
                    "level": "error",
                    "text": self._setup_detail or "No LLM provider is configured.",
                },
            )
            await self._finish_turn(websocket, turn_id, status="failed", actions=[])
            return

        self._turn_id = self._allocate_turn_id()
        self._turn_task = asyncio.create_task(
            self._run_turn(websocket, self._turn_id, text),
            name=f"sidecar-turn-{self._turn_id}",
        )

    def _allocate_turn_id(self) -> str:
        self._next_turn += 1
        return f"t-{self._next_turn}"

    async def _run_turn(self, websocket: ServerConnection, turn_id: str, text: str) -> None:
        """Run the tool loop and report it as protocol events (SPEC.md 5.2).

        The event sequence lives here rather than in the loop so that every exit
        path -- completed, failed, cancelled -- sends exactly one `turn_finished`,
        which is what the panel needs to leave its thinking state.
        """
        message_id = f"a-{turn_id}"
        try:
            await self._send_event(
                websocket,
                "turn_started",
                {"turn_id": turn_id, "message_id": message_id},
            )

            loop = agent.ToolLoop(
                link=self,
                provider=self._provider,
                catalog=self._catalog,
                history=self._history,
                max_tool_rounds=self.config.max_tool_rounds,
                max_objects_per_turn=self.config.max_objects_per_turn,
                turn_timeout_s=self.config.turn_timeout_s,
                turn_token_budget=self.config.turn_token_budget,
                max_retries=self.config.provider_max_retries,
                retry_base_s=self.config.provider_retry_base_s,
            )
            result = await loop.run(turn_id=turn_id, message_id=message_id, user_text=text)

            if result.text:
                # Sent last as well as any interim text, so the panel's final bubble
                # is the complete answer rather than whatever arrived mid-turn.
                await self._send_event(
                    websocket,
                    "assistant_message",
                    {"turn_id": turn_id, "message_id": message_id, "text": result.text},
                )

            await self._finish_turn(
                websocket,
                turn_id,
                status=result.status,
                actions=result.actions_json,
                txn_id=result.txn_id,
                usage=result.usage.to_json(),
            )
        except asyncio.CancelledError:
            # The loop has already closed its transaction by the time this runs
            # (it re-raises only after a shielded cleanup), so reporting the
            # cancellation is all that is left.
            await self._finish_turn(websocket, turn_id, status="cancelled", actions=[])
            raise
        finally:
            if self._turn_id == turn_id:
                self._turn_id = None
                self._turn_task = None

    async def _finish_turn(
        self,
        websocket: ServerConnection,
        turn_id: str,
        *,
        status: str,
        actions: list[dict],
        txn_id: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        """Send `turn_finished`, tolerating a socket that has already gone."""
        payload: dict[str, Any] = {
            "turn_id": turn_id,
            "status": status,
            "actions": actions,
            # Null when the turn never mutated anything, which is the normal case
            # for a read-only turn: the panel must not offer Revert for it.
            "txn_id": txn_id,
        }
        if usage is not None:
            payload["usage"] = usage
        try:
            await self._send_event(websocket, "turn_finished", payload)
        except ConnectionClosed:
            log.info("turn %r finished as %r after the client had gone", turn_id, status)

    async def _handle_cancel_turn(self, websocket: ServerConnection, payload: dict) -> None:
        turn_id = payload.get("turn_id")
        task = self._turn_task
        if task is None or task.done() or turn_id != self._turn_id:
            # Cancelling a turn that is not running is not an error: the user may
            # have pressed Stop just as the turn finished, and the two crossed on
            # the wire.
            log.info("cancel_turn for %r, which is not running", turn_id)
            return
        log.info("cancelling turn %r", turn_id)
        task.cancel()

    async def _cancel_running_turn(self, *, disconnected: bool) -> None:
        """Stop the running turn, if any, and wait for it to unwind."""
        task = self._turn_task
        if task is None or task.done():
            return
        if disconnected:
            # Nobody is left to hear the `turn_finished`, and the socket is
            # closed, so the task's own report would only log a spurious error.
            log.info("editor closed mid-turn; turn %r is incomplete", self._turn_id)
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

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

    async def send_event(self, type_name: str, payload: dict[str, Any]) -> None:
        """Public event send, for `agent.ToolLoop`.

        Wraps `_send_event` so the loop never touches the socket handle directly.
        Missing client is not an error here: the loop may finish a turn just as the
        editor closes, and the events it sends at that point have nobody to reach.
        """
        websocket = self._client
        if websocket is None:
            log.debug("dropping %s event: no client connected", type_name)
            return
        with contextlib.suppress(ConnectionClosed):
            await self._send_event(websocket, type_name, payload)

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
        if self._provider is None:
            log.warning(
                "no provider configured; the panel will show needs_setup: %s",
                self._setup_detail,
            )
        else:
            log.info("provider ready: %s", self._provider.name)

        async with serve(
            self.handler,
            self.config.ws_host,
            self.config.ws_port,
            process_request=self.rejects_origin,
        ):
            await asyncio.Future()  # run until cancelled
