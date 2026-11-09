"""The LLM tool loop: one turn, from user message to `turn_finished` (SPEC.md 5.2).

The loop is deliberately transport-agnostic. It talks to the editor through a
narrow `EditorLink` and to the model through a `Provider`, which is what lets the
tests drive a complete turn -- tool calls, transactions, caps, cancellation --
without a WebSocket or a network.

Transaction handling is the part worth reading carefully. SPEC.md 5.2 requires one
transaction per turn, opened *lazily* before the first mutation and ended always.
Lazy matters: a read-only turn that opened one would leave an empty undo entry in
the user's history, so Ctrl+Z would appear to do nothing. "Ended always" matters
more: an abandoned transaction stays open in the mod, and every later turn's
`begin_transaction` then fails with `TRANSACTION_ACTIVE` -- so this is a
correctness requirement, not tidiness. It is enforced in a `finally` and, for
cancellation, through a shielded task, because the cleanup has to survive the
cancellation that triggered it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import capture, properties_table, save_properties, tools
from . import catalog as catalog_module
from .agent_prompt import system_prompt
from .design import grid_tools
from .design import render as render_module
from .design.coords import Grid, GroundUnknownError
from .design.level_model import LevelChangingError, SnapshotCache, SnapshotError
from .design.roles import load_role_table
from .design.sim import playtest as playtest_module
from .design.sim import verify as verify_module
from .providers.base import (
    AssistantTurn,
    Message,
    Provider,
    ProviderError,
    ToolCall,
    Usage,
)

log = logging.getLogger("sidecar")

#: Turn statuses (SPEC.md 7.4). Named constants because they are compared against
#: strings in tests and the UI, and a typo in a literal is silent.
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
INCOMPLETE = "incomplete"

#: How many object ids to ask for per `list_object_catalog` request.
#: Large enough that the whole table arrives in a handful of round trips, small enough
#: that one frame stays far inside the protocol's 1 MB limit.
_CATALOG_PAGE_SIZE = 500

#: `playtest_verify` runs only when the user asked for an in-game test in their message
#: (LEVEL-DESIGN-SPEC.md 7.3: the game plays by itself while it runs). The words are
#: English only for now; until the plan's `allow_replay` exists (M4), this is the gate.
_ASKED_TO_PLAY = re.compile(r"\b(play|playtest|playtesting|test|testing|try it|run it)\b",
                            re.IGNORECASE)
_MAX_TURN_PLAYTESTS = 2
#: Replays one turn may run in all: each is a real playtest of the section.
_MAX_TURN_REPLAYS = 12

#: Cap on pages fetched in one go. A guard against a mod that never reports the last
#: page, which would otherwise loop until the turn's wall-clock timeout.
_MAX_CATALOG_PAGES = 40


class EditorLink(Protocol):
    """What the loop needs from the connection to the mod."""

    async def request(self, type_name: str, payload: dict[str, Any] | None = None,
                      timeout: float | None = None) -> Any:
        """Send a request and await its response.

        Must return an object with `ok: bool`, `result: dict | None` and
        `error: dict | None` -- the sidecar's `Response` satisfies this. The loop
        is written against the shape rather than the class so tests can supply a
        fake.
        """

    async def send_event(self, type_name: str, payload: dict[str, Any]) -> None:
        """Send an event that expects no reply."""


@dataclass
class ActionRecord:
    """One entry of `turn_finished.actions` (SPEC.md 7.4).

    `kind` is a tool name, not a verb: the schema pins it to the mutating tools.
    """

    kind: str
    count: int
    summary: str

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "count": self.count, "summary": self.summary}


@dataclass
class TurnResult:
    """Everything the caller needs to report the turn."""

    status: str
    text: str = ""
    actions: list[ActionRecord] = field(default_factory=list)
    txn_id: str | None = None
    usage: Usage = field(default_factory=Usage)

    @property
    def actions_json(self) -> list[dict[str, Any]]:
        return [action.to_json() for action in self.actions]


# What the model is told when the level changed behind its back (SPEC.md 6.1.7).
#
# Two notes rather than one, because the useful inference differs. A hand edit means
# "your picture is stale". A revert means "the work you just did is gone" -- a claim
# the model will otherwise contradict, since its own transcript says it succeeded.
HAND_EDIT_NOTE = (
    "[system note] The level changed outside the assistant since the last turn: "
    "objects were added, modified or removed by hand. Do not assume anything you "
    "were told or did earlier is still true. Re-query the editor state before "
    "acting on it."
)

REVERTED_NOTE = (
    "[system note] The user undid your last turn with the Revert action. Those "
    "changes are gone: objects you created no longer exist, and anything you "
    "modified is back to how it was. Do not describe that work as done, and do not "
    "reuse any object ids from it. Re-query the editor state if you need to know "
    "what is there now."
)


class History:
    """Conversation history for one session (SPEC.md 6.2.3).

    Keeps only user and assistant *text*: tool calls and their results are dropped
    once a turn ends. That is the spec's rule and it is also the right one -- a
    stale tool result is a statement about a level state that the user has since
    edited, and feeding it back invites the model to reason from fiction. Anything
    it needs it can re-query.

    The transcript also survives the user editing by hand, which is deliberate:
    closing and reopening a level should not erase the conversation. But the level
    changing underneath it is *not* harmless, so `mark_level_changed` records that
    the assistant's picture of the level is out of date, and the loop tells the
    model so at the start of the next turn.

    Without that, a user who deletes everything and asks "now add a platform above
    the spikes" would be answered against a level that no longer has spikes, and
    the model has no way to notice. Sending a note is cheaper and more reliable
    than expecting it to re-query something it believes it already knows.
    """

    def __init__(self, token_budget: int) -> None:
        self._messages: list[Message] = []
        self._token_budget = token_budget
        self._level_changed: str | None = None

    def __len__(self) -> int:
        return len(self._messages)

    def messages(self) -> list[Message]:
        return list(self._messages)

    def append(self, user_text: str, assistant_text: str) -> None:
        if user_text:
            self._messages.append(Message(role="user", text=user_text))
        if assistant_text:
            self._messages.append(Message(role="assistant", text=assistant_text))
        self._trim()

    def clear(self) -> None:
        self._messages.clear()
        self._level_changed = None

    def mark_level_changed(self, note: str = HAND_EDIT_NOTE) -> None:
        """Record that the level changed outside the assistant.

        The note is what the model will be told, and the two causes are worth
        separating. After a hand edit the level is merely unknown. After a revert it
        is *known to be missing this assistant's work*, and an object id it created no
        longer exists -- which is exactly the kind of fiction that makes a follow-up
        turn fail. Saying only "something changed" makes the model re-query and then
        guess which of its own objects survived.

        A pending revert note is **not** downgraded to a hand-edit note. It would be,
        without the check below: a revert changes the object count, so the mod's own
        change detector reports a `level_changed` a moment after the
        `transaction_reverted`, and "later wins" would then throw away the sharper
        note in favour of the vaguer one.
        """
        if self._level_changed == REVERTED_NOTE and note == HAND_EDIT_NOTE:
            return
        self._level_changed = note

    def take_level_changed(self) -> str | None:
        """The note recorded since the last call, then reset.

        Consumed rather than sticky: the note is about a specific change, and
        repeating it every turn would teach the model to ignore it. The most recent
        reason wins when several arrive in one turn -- it is the only one still true.
        """
        note = self._level_changed
        self._level_changed = None
        return note

    def _trim(self) -> None:
        """Drop the oldest messages until the budget is met.

        Roughly four characters per token, which is enough for a bound that exists
        to stop unbounded growth. Whitespace is not stripped first, so the estimate
        errs high, and an over-eager trim is better than an oversized request.
        """

        def cost(message: Message) -> int:
            return len(message.text) // 4 + 8

        total = sum(cost(message) for message in self._messages)
        while self._messages and total > self._token_budget:
            total -= cost(self._messages.pop(0))


class ToolLoop:
    """Runs one turn: model, tools, transaction, limits, result."""

    def __init__(
        self,
        *,
        link: EditorLink,
        provider: Provider,
        catalog: catalog_module.Catalog,
        history: History,
        max_tool_rounds: int = 10,
        max_objects_per_turn: int = 5000,
        turn_timeout_s: int = 120,
        turn_token_budget: int | None = None,
        max_retries: int = 3,
        retry_base_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._link = link
        self._provider = provider
        self._catalog = catalog
        self._history = history
        self._max_tool_rounds = max_tool_rounds
        self._max_objects = max_objects_per_turn
        self._timeout_s = turn_timeout_s
        self._token_budget = turn_token_budget
        self._max_retries = max_retries
        self._retry_base_s = retry_base_s
        self._sleep = sleep

        # Per-turn state, reset by `run`.
        self._turn_id = ""
        self._message_id = ""
        self._actions: list[ActionRecord] = []
        self._usage = Usage()
        self._txn_id: str | None = None
        self._objects_used = 0
        self._text = ""
        self._user_text = ""
        self._playtests = 0

        # Object ids that changed because a save-string write rebuilt the object.
        #
        # Kept for the turn and applied to every later call's ids, because a model that
        # keeps using a stale id gets OBJECT_NOT_FOUND for an object it can see in its
        # own earlier results -- a dead end it cannot diagnose. Never invents an id;
        # only follows one that was reported.
        self._renamed_ids: dict[int, int] = {}

        # Images produced by captures during this turn. Attached to the next model call and
        # then dropped -- see `_with_pending_images` for why they are drained, not kept.
        self._pending_images: list[str] = []

        # Request types the connected mod has said it does not implement. Shared with the
        # link when it offers a set, so the knowledge outlives this turn but not the
        # connection (the server replaces the set when a mod connects).
        shared = getattr(link, "unsupported_requests", None)
        self._unsupported: set[str] = shared if isinstance(shared, set) else set()

        # The level snapshot for design tools (LEVEL-DESIGN-SPEC.md 4.3.3): built on first
        # use, dropped after any mutation and at the end of the turn.
        self._snapshot = SnapshotCache(link, load_role_table())

    # -- public entry point ---------------------------------------------

    async def run(self, *, turn_id: str, message_id: str, user_text: str) -> TurnResult:
        self._reset(turn_id, message_id)

        try:
            async with asyncio.timeout(self._timeout_s):
                return await self._drive(user_text)
        except TimeoutError:
            log.warning("turn %s hit the %ss wall-clock limit", turn_id, self._timeout_s)
            await self._end_transaction()
            return self._result(
                INCOMPLETE,
                self._text
                or f"Stopped after {self._timeout_s}s. Some of the work may not have been done.",
            )
        except asyncio.CancelledError:
            # The cleanup has to survive the cancellation that caused it, so it
            # runs shielded in its own task: without that, the `end_transaction`
            # request would be cancelled mid-flight and the transaction would stay
            # open in the mod, breaking every later turn.
            await self._end_transaction(shielded=True)
            raise
        except ProviderError as exc:
            log.warning("turn %s failed at the provider: %s", turn_id, exc)
            await self._end_transaction()
            return self._result(FAILED, self._failure_text(exc))

    # -- the loop --------------------------------------------------------

    async def _drive(self, user_text: str) -> TurnResult:
        self._user_text = user_text
        messages = self._history.messages()

        # Announce a hand edit *before* the user's message, so the model reads it as
        # context rather than as something the user just said.
        #
        # Only on a changed level, and only as a note: the alternative -- clearing
        # history -- would throw away a conversation the user can still see on
        # screen, which is worse. See `History`.
        change_note = self._history.take_level_changed()
        if change_note is not None:
            messages.append(Message(role="user", text=change_note))

        messages.append(Message(role="user", text=user_text))

        status = COMPLETED
        for _round in range(self._max_tool_rounds):
            turn = await self._complete(messages)
            self._usage = self._usage + Usage(
                input_tokens=turn.usage.input_tokens,
                output_tokens=turn.usage.output_tokens,
                tool_rounds=1,
            )

            if self._token_budget is not None and self._usage.total_tokens > self._token_budget:
                log.warning("turn %s exceeded the token budget", self._turn_id)
                status = INCOMPLETE
                break

            if turn.text:
                # Interim text is sent as it arrives and again if it changes, under
                # one message id, so the panel shows a single bubble that fills in
                # rather than a stack of partial ones (SPEC.md 7.4).
                self._text = turn.text
                await self._emit_assistant_text(turn.text)

            if not turn.wants_tools:
                break

            messages.append(
                Message(role="assistant", text=turn.text, tool_calls=list(turn.tool_calls))
            )

            # Tool calls in one round run sequentially. They are not independent:
            # a create followed by a modify of the created objects is a normal
            # pattern, and ordering is the only thing that makes it work.
            stop = False
            for call in turn.tool_calls:
                result, is_error = await self._execute(call)
                messages.append(
                    Message(
                        role="tool",
                        text=_dump_result(result),
                        tool_call_id=call.id,
                        is_error=is_error,
                    )
                )
                if result.get("_limit_reached"):
                    status = INCOMPLETE
                    stop = True
            if stop:
                break
        else:
            # The `else` on a `for` runs when it completes without `break`, i.e.
            # the round cap was reached with the model still asking for tools.
            log.warning("turn %s hit the %s-round cap", self._turn_id, self._max_tool_rounds)
            status = INCOMPLETE
            self._text = self._text or (
                f"Stopped after {self._max_tool_rounds} rounds of tool calls. "
                "The level may be partly edited; review it before continuing."
            )

        txn_id = await self._end_transaction()
        if not self._text:
            self._text = self._summarise_silently(status)

        # Only a completed turn becomes history. An interrupted one would be
        # repeated as fact on the next turn, and its edits may have been reverted.
        if status == COMPLETED:
            self._history.append(user_text, self._text)

        return self._result(status, self._text, txn_id=txn_id)

    async def _ensure_catalog(self) -> None:
        """Read the game's own object table, once per sidecar process.

        `list_object_catalog` is answered out of `ObjectToolbox`, which is the table
        the game itself uses to map an id to a sprite. That makes every entry real,
        which is the only property that matters here: a guessed id does not fail
        loudly, it places the wrong object in the user's level (SPEC.md 15).

        Failure is not fatal and is not retried forever. An older mod answers with an
        error, and a turn that cannot look up ids is still a usable turn -- what is
        not acceptable is paying a failed round trip at the start of every turn.
        """
        if not self._catalog.fetch_allowed:
            return

        self._catalog.fetch_attempts += 1
        collected: list[dict[str, Any]] = []
        offset = 0

        for _ in range(_MAX_CATALOG_PAGES):
            response = await self._link.request(
                "list_object_catalog", {"offset": offset, "limit": _CATALOG_PAGE_SIZE}
            )
            if not response.ok:
                error = response.error or {}
                log.warning(
                    "catalog: the mod could not list object types (%s); keeping %d entries",
                    error.get("message", "request failed"),
                    self._catalog.size,
                )
                return

            result = response.result if isinstance(response.result, dict) else {}
            collected.extend(
                entry for entry in (result.get("entries") or []) if isinstance(entry, dict)
            )

            next_offset = result.get("next_offset")
            # `not isinstance(..., int)` covers both a null (last page) and a mod that
            # omitted the field; `<= offset` stops a mod that repeats its own offset.
            if not isinstance(next_offset, int) or next_offset <= offset:
                break
            offset = next_offset

        if not collected:
            log.warning("catalog: the mod reported no object types; keeping the seed")
            return

        added = self._catalog.absorb_game_table(collected)
        log.info(
            "catalog: %d object ids from the game (%d new, %d total)",
            len(collected),
            added,
            self._catalog.size,
        )

    async def _complete(self, messages: list[Message]) -> AssistantTurn:
        """Call the provider, retrying a retryable failure (SPEC.md 6.2.3)."""
        attempt = 0
        while True:
            try:
                return await self._provider.complete(
                    system=system_prompt(),
                    messages=self._with_pending_images(messages),
                    tools=tools.tool_specs(self._unsupported),
                )
            except ProviderError as exc:
                attempt += 1
                if not exc.retryable or attempt > self._max_retries:
                    raise
                # Exponential backoff with jitter. The jitter is what stops a
                # provider that is failing for everything from being hit by every
                # retry at the same instant.
                delay = self._retry_base_s * (2 ** (attempt - 1))
                delay += random.uniform(0, self._retry_base_s)
                log.warning(
                    "provider call failed (attempt %d/%d), retrying in %.2fs: %s",
                    attempt,
                    self._max_retries,
                    delay,
                    exc,
                )
                await self._sleep(delay)

    # -- tool execution --------------------------------------------------

    async def _attach_capture(self, result: dict[str, Any]) -> dict[str, Any]:
        """Queue a captured screenshot as an image on the next model call.

        The image cannot ride in the tool result. Providers accept images in **user**
        messages and reject them in system/assistant ones, and a tool result has a role of
        its own that the documentation does not cover -- so the picture is appended as its
        own user message instead, with a line saying what it shows. That keeps the request
        inside the documented contract rather than depending on how a tool role happens to
        be treated.

        `pending` is drained by `_complete`, so every image produced during a turn is
        attached to the next call and then dropped. Dropping matters: history keeps only
        user and assistant *text*, and a stale screenshot would be a picture of a level
        state that no longer exists -- the same trap as a stale tool result.
        """
        described = capture.describe_capture(result)
        path = result.get("path")
        url, extra = capture.load_capture(path if isinstance(path, str) else "")
        described.update(extra)

        if url is None:
            # Said, not swallowed: a model that is not told the capture failed will describe
            # a level it never saw.
            log.warning("capture failed: %s", described.get("error"))
            described["note"] = (
                "The capture failed, so you have NOT seen the level. Do not describe it. "
                "Tell the user the screenshot did not work."
            )
            return described

        self._pending_images.append(url)
        described["note"] = "The image is attached to your next message."
        return described

    async def _report_progress(self, tool_name: str) -> None:
        """Best-effort: a missing progress event must never break a turn.

        Sending an event is the one place in the loop where a transport failure has
        nothing to do with the work being done. If it fails, the turn continues -- the
        user loses a label, not the edits.
        """
        try:
            await self._link.send_event("tool_call", {"turn_id": self._turn_id, "tool": tool_name})
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see above
            log.debug("could not report progress for %s: %s", tool_name, exc)

    def _with_pending_images(self, messages: list[Message]) -> list[Message]:
        """Return `messages` with any captured images attached as a user message.

        Drained, not kept: the picture is about the level *now*, and the next turn's history
        holds only text. A retained screenshot would be a picture of a state the user has
        since changed -- the same reason tool results are dropped once a turn ends.
        """
        if not self._pending_images:
            return messages

        images = self._pending_images
        self._pending_images = []
        log.info("attaching %d captured image(s) to the next model call", len(images))

        # Appended rather than merged into the last user message: that message is already
        # part of the conversation the provider has seen, and rewriting it would change
        # history retroactively.
        return [
            *messages,
            Message(
                role="user",
                text=(
                    "Here is the capture you asked for. Judge only what is visible; if "
                    "something is unclear or cut off, say so rather than guessing."
                ),
                images=images,
            ),
        ]

    async def _execute(self, call: ToolCall) -> tuple[dict[str, Any], bool]:
        # Tell the panel what is happening before doing it, not after.
        #
        # A turn can run forty rounds, and the panel shows one indicator heading for all
        # of them, so without this a working turn and a frozen one look identical. Sent
        # before the call so the name appears while the work is in flight -- after would
        # only narrate history.
        await self._report_progress(call.name)

        tool = tools.get(call.name)
        if tool is None:
            # A hallucinated tool name is recoverable information, not a reason to
            # kill the turn (see `tools.unknown_tool_names`).
            return (
                {
                    "error": f"unknown tool {call.name!r}",
                    "available": list(tools.TOOLS),
                },
                True,
            )

        # Repair stale ids before anything looks at them. A save-string write rebuilds the
        # object, so the id the model is holding may no longer exist; following the
        # reported replacement keeps its next call working instead of failing with
        # OBJECT_NOT_FOUND on an object it can see in its own earlier results.
        if self._renamed_ids and isinstance(call.arguments, dict):
            remapped = self._remap_ids(call.arguments)
            if remapped != call.arguments:
                log.info(
                    "remapped stale object ids for %s: %s",
                    call.name,
                    {old: new for old, new in self._renamed_ids.items()},
                )
                call = ToolCall(id=call.id, name=call.name, arguments=remapped)

        if tool.protocol_type is None:
            # Local tools: no editor round trip. Dispatched by name because more than one
            # tool now lives here, and falling through to the catalog for all of them
            # would answer a property query with a list of object ids.
            if tool.spec.name == "search_properties":
                return properties_table.search_properties(call.arguments), False
            if tool.spec.name == "describe_class":
                return properties_table.describe_class(call.arguments), False
            if tool.spec.name in tools.DESIGN_TOOLS:
                return await self._run_design_tool(tool, call)
            # Lazy, and only here. Reading the game's object table costs a handful of
            # round trips, and most turns never look an id up -- paying for it at the
            # start of every turn would slow down "delete the spikes" for nothing.
            await self._ensure_catalog()
            return catalog_module.search_object_catalog(self._catalog, call.arguments), False

        if tool.mutates_objects:
            wanted = tool.object_count(call.arguments)
            if self._objects_used + wanted > self._max_objects:
                # Refused, and the turn ends: letting the model retry would just
                # have it slice the work into pieces that each pass the check,
                # which defeats the cap rather than enforcing it.
                return (
                    {
                        "error": (
                            f"refused: this call would take the turn to "
                            f"{self._objects_used + wanted} objects, over the "
                            f"{self._max_objects} limit"
                        ),
                        "_limit_reached": True,
                    },
                    True,
                )

        if tool.mutates_objects and self._txn_id is None:
            opened, is_error = await self._begin_transaction()
            if is_error:
                return opened, True

        # Save-property tools need work on this side before the request goes out, and one
        # of them needs post-processing, so they are not a plain pass-through.
        if tool.spec.name == "set_save_property":
            prepared = await self._prepare_set_save_property(call.arguments)
            if "error" in prepared:
                return prepared, True
            call = ToolCall(id=call.id, name=call.name, arguments=prepared["arguments"])
            result, is_error = await self._call_mod(tool, call, prepared)
            return result, is_error

        response = await self._link.request(tool.protocol_type, call.arguments)
        if not response.ok:
            return self._request_failed(tool.protocol_type, response), True

        if tool.mutates_objects:
            self._snapshot.invalidate()

        result = response.result if isinstance(response.result, dict) else {}
        if tool.spec.name == "get_save_properties":
            result = save_properties.describe_save_properties(result)
        if tool.spec.name == "capture_screenshot":
            result = await self._attach_capture(result)
        if tool.mutates_objects:
            self._objects_used += tool.object_count(call.arguments)
        if tool.is_action:
            self._actions.append(_describe(tool.spec.name, call.arguments, result))
        return result, False

    async def _run_design_tool(
        self, tool: tools.Tool, call: ToolCall
    ) -> tuple[dict[str, Any], bool]:
        """GridText and schematic tools: computed here from the level snapshot.

        `place_grid` is the one that edits, and it goes through the same machinery as
        `create_objects`: the per-turn cap (counted before anything is sent), the turn's
        lazy transaction, and plain `delete_objects` / `create_objects` requests, so the
        mod needs nothing new and the result is one undo step with the rest of the turn.
        """
        args = call.arguments if isinstance(call.arguments, dict) else {}
        try:
            grid = Grid.from_constants()
            model = await self._snapshot.get()
        except GroundUnknownError as exc:
            return {"error": str(exc)}, True
        except LevelChangingError:
            return {"error": "the level kept changing while it was being read; try again "
                    "once the user has stopped editing"}, True
        except SnapshotError as exc:
            if exc.code == "UNSUPPORTED_REQUEST":
                self._unsupported.add("get_level_objects")
            return {"error": f"could not read the level: {exc}"}, True

        name = tool.spec.name
        if name == "render_grid":
            result = grid_tools.render_grid(model, grid, args)
            return result, "error" in result

        if name == "verify_section":
            result = verify_module.verify_section(model, args)
            return result, "error" in result

        if name == "playtest_verify":
            refused = self._playtest_refusal()
            if refused is not None:
                return {"error": refused}, True
            self._playtests += 1
            args = {**args, "max_replays": min(int(args.get("max_replays", 12)),
                                               _MAX_TURN_REPLAYS)}
            result = await playtest_module.playtest_verify(self._link.request, model, args)
            return result, result.get("result") == "error" or "error" in result

        if name == "render_schematic":
            try:
                overlays = set(args.get("overlays") or ())
                route = None
                if overlays & {"route", "windows"} and args.get("x_range"):
                    route = verify_module.route_overlay(
                        model, max(float(v) for v in args["x_range"]))
                schematic = render_module.render_schematic(model, grid, args, route)
            except render_module.SchematicError as exc:
                return {"error": str(exc)}, True
            if len(schematic.png) > capture.MAX_FILE_BYTES:
                return {"error": "the schematic is too large; narrow the range"}, True
            encoded = base64.b64encode(schematic.png).decode("ascii")
            self._pending_images.append(f"data:image/png;base64,{encoded}")
            result = schematic.describe(grid)
            result["note"] = "The image is attached to your next message."
            return result, False

        plan = grid_tools.plan_place_grid(model, grid, self._snapshot.roles, args)
        if isinstance(plan, dict):
            return plan, True
        verification = None
        if plan.verify is not None:
            # Verified against the level as it would stand, so it works for a preview
            # too, and computed before the edit so a failure is reported either way.
            verification = verify_module.verify_placement(
                model, plan.creates, plan.deletes, plan.block_x, plan.verify)
        if plan.preview:
            preview = plan.preview_result()
            if verification is not None:
                preview["verification"] = verification
            return preview, False
        if not plan.creates and not plan.deletes:
            return {"created": 0, "deleted": 0, "notes": ["the grid is empty"]}, False
        if self._objects_used + plan.object_count > self._max_objects:
            return (
                {
                    "error": (
                        f"refused: this grid would take the turn to "
                        f"{self._objects_used + plan.object_count} objects, over the "
                        f"{self._max_objects} limit"
                    ),
                    "_limit_reached": True,
                },
                True,
            )
        if self._txn_id is None:
            opened, is_error = await self._begin_transaction()
            if is_error:
                return opened, True

        deleted = 0
        if plan.deletes:
            response = await self._link.request("delete_objects", {"targets": plan.deletes})
            self._snapshot.invalidate()
            if not response.ok:
                return self._request_failed("delete_objects", response), True
            delete_result = response.result if isinstance(response.result, dict) else {}
            deleted = int(delete_result.get("deleted_count", len(plan.deletes)) or 0)
            self._objects_used += len(plan.deletes)
            self._actions.append(
                _describe("delete_objects", {"targets": plan.deletes}, delete_result)
            )

        created: list[int] = []
        if plan.creates:
            payload = {"objects": plan.creates}
            response = await self._link.request("create_objects", payload)
            self._snapshot.invalidate()
            if not response.ok:
                failed = self._request_failed("create_objects", response)
                if deleted:
                    failed["note"] = (f"{deleted} objects were already deleted before the "
                                      "create failed; the turn's undo restores them")
                return failed, True
            create_result = response.result if isinstance(response.result, dict) else {}
            created = [int(u) for u in create_result.get("created") or []]
            self._objects_used += len(plan.creates)
            self._actions.append(_describe("create_objects", payload, create_result))

        result: dict[str, Any] = {"created": len(created), "deleted": deleted,
                                  "created_uids": created}
        if verification is not None:
            result["verification"] = verification
        if plan.notes:
            result["notes"] = plan.notes
        return result, False

    def _request_failed(self, request_type: str, response: Any) -> dict[str, Any]:
        """A failed mod request as a tool result, remembering unsupported requests.

        `UNSUPPORTED_REQUEST` means the connected mod is older than this sidecar. The
        request type is recorded on the link, so every later call for this connection
        -- this turn and the next -- leaves out the tools that depend on it
        (LEVEL-DESIGN-SPEC.md LD14), and the model is told plainly rather than left to
        retry something that cannot work.
        """
        error = response.error or {}
        code = error.get("code", "INTERNAL_ERROR")
        result: dict[str, Any] = {
            "error": error.get("message", "the request failed"),
            "code": code,
        }
        if code == "UNSUPPORTED_REQUEST":
            self._unsupported.add(request_type)
            log.warning("mod does not implement %r; hiding dependent tools", request_type)
            result["note"] = (
                "The connected version of the mod does not support this. The tool has "
                "been removed for this session; tell the user it needs a newer mod."
            )
        return result

    def _playtest_refusal(self) -> str | None:
        """Why `playtest_verify` may not run now, or None (LEVEL-DESIGN-SPEC.md 7.3)."""
        if not _ASKED_TO_PLAY.search(self._user_text or ""):
            return ("refused: playtest_verify runs only when the user asks for a playtest "
                    "or an in-game test in their message; the game plays by itself while "
                    "it runs. Offer it instead.")
        if self._txn_id is not None:
            return ("refused: this turn has already changed the level, and a replay needs "
                    "the edits finished. End the turn and offer to playtest next.")
        if self._playtests >= _MAX_TURN_PLAYTESTS:
            return f"refused: at most {_MAX_TURN_PLAYTESTS} playtest_verify calls per turn"
        return None

    async def _prepare_set_save_property(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Turn a property *name* into a key, and refuse keys the mod will reject.

        Both are done here rather than in the mod because only the sidecar has the
        generated name table (SPEC.md 7.5.3). Catching a reserved key before the request
        is worth the round trip: the mod's refusal would otherwise arrive after the
        object had already been looked up, and the message the model sees would be a
        generic error rather than "use modify_objects instead".
        """
        raw_key = arguments.get("key")
        if raw_key is None:
            key, info = save_properties.resolve_key(arguments.get("property"))
        else:
            key, info = save_properties.resolve_key(raw_key)

        if key is None:
            return {"error": info.get("error", "could not resolve that property name"), **info}

        blocked = save_properties.check_writable(key)
        if blocked is not None:
            return {"error": f"key {key} cannot be written this way: {blocked}"}

        if "value" not in arguments:
            return {"error": "'value' is required"}

        table = properties_table.load_table()
        name = table.name_for_key(key)
        kind = None
        if name:
            for entry in table.all_entries():
                if entry.key == key:
                    kind = entry.type
                    break

        # Rebuild the arguments the mod sees: key resolved, value encoded.
        forwarded = {
            "targets": list(arguments.get("targets") or []),
            "key": key,
            "value": save_properties.encode_value(arguments["value"], kind),
        }
        if not forwarded["targets"]:
            return {"error": "'targets' must be a non-empty list of ids"}

        return {
            "arguments": forwarded,
            "resolved": {"key": key, "name": name, "type": kind, **info},
        }

    async def _call_mod(
        self, tool: tools.Tool, call: ToolCall, prepared: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Send a prepared save-property request and annotate the result."""
        assert tool.protocol_type is not None
        response = await self._link.request(tool.protocol_type, call.arguments)
        if not response.ok:
            return self._request_failed(tool.protocol_type, response), True

        self._snapshot.invalidate()
        result = dict(response.result) if isinstance(response.result, dict) else {}
        resolved = prepared.get("resolved", {})
        result["property"] = resolved

        # Make the id change impossible to miss. The model has to carry on with the new
        # ids, and a replacement list buried in a result object is easy to skim past --
        # so it is also spelled out, and remembered for the rest of the turn so a later
        # call can be corrected automatically.
        replacements = result.get("replacements") or []
        for item in replacements:
            old_id, new_id = item.get("old_id"), item.get("new_id")
            if isinstance(old_id, int) and isinstance(new_id, int):
                self._renamed_ids[old_id] = new_id
        if replacements:
            result["note"] = (
                "The object(s) were rebuilt, so their ids changed. Use the `new_id` "
                "values from here on; the old ids no longer exist."
            )

        self._objects_used += tool.object_count(call.arguments)
        if tool.is_action:
            self._actions.append(_describe(tool.spec.name, call.arguments, result))
        return result, False

    def _remap_ids(self, arguments: Any) -> Any:
        """Follow object ids that changed because of a save-property write.

        A rebuilt object has a new id, and a model that keeps using the old one gets
        OBJECT_NOT_FOUND for an object it can see in its own earlier results. Remapping
        here turns that dead end into a working call, and is only ever a *repair*: it
        never invents an id, only follows one that was reported.
        """
        if not self._renamed_ids:
            return arguments
        if isinstance(arguments, dict):
            remapped = {}
            for key, value in arguments.items():
                if key in {"targets", "object_id"} and isinstance(value, int):
                    remapped[key] = self._renamed_ids.get(value, value)
                elif key in {"targets", "ids"} and isinstance(value, list):
                    remapped[key] = [
                        self._renamed_ids.get(item, item) if isinstance(item, int) else item
                        for item in value
                    ]
                else:
                    remapped[key] = self._remap_ids(value)
            return remapped
        if isinstance(arguments, list):
            return [self._remap_ids(item) for item in arguments]
        return arguments

    async def _begin_transaction(self) -> tuple[dict[str, Any], bool]:
        response = await self._link.request("begin_transaction", {"label": self._turn_id})
        if not response.ok:
            error = response.error or {}
            # Most often TRANSACTION_ACTIVE, which means a previous turn leaked
            # one. Worth saying plainly: it is a bug, and silently retrying would
            # hide it.
            return (
                {
                    "error": error.get("message", "could not open a transaction"),
                    "code": error.get("code", "INTERNAL_ERROR"),
                },
                True,
            )
        self._txn_id = (response.result or {}).get("txn_id")
        log.info("turn %s opened transaction %s", self._turn_id, self._txn_id)
        return {"txn_id": self._txn_id}, False

    async def _end_transaction(self, *, shielded: bool = False) -> str | None:
        """Close the open transaction, if any. Idempotent.

        Returns the transaction id, or None when there was none -- which is the
        normal case for a read-only turn.
        """
        txn_id = self._txn_id
        self._txn_id = None
        if txn_id is None:
            return None

        async def close() -> None:
            response = await self._link.request("end_transaction", {"txn_id": txn_id})
            if response.ok:
                log.info("turn %s closed transaction %s", self._turn_id, txn_id)
            else:
                # Loud, because an unclosed transaction blocks every later turn.
                log.error(
                    "turn %s could not close transaction %s: %s",
                    self._turn_id,
                    txn_id,
                    (response.error or {}).get("message"),
                )

        if shielded:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                # A short timeout inside the shield: the whole point is to finish
                # this before the task dies, and waiting indefinitely would hang
                # the cancellation instead.
                await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(close())), timeout=5)
        else:
            with contextlib.suppress(Exception):
                await close()

        return txn_id

    # -- helpers ---------------------------------------------------------

    def _reset(self, turn_id: str, message_id: str) -> None:
        self._turn_id = turn_id
        self._message_id = message_id
        self._actions = []
        self._usage = Usage()
        self._txn_id = None
        self._objects_used = 0
        self._text = ""
        self._renamed_ids = {}
        self._pending_images = []
        self._playtests = 0
        self._user_text = ""
        self._snapshot.invalidate()

    def _result(self, status: str, text: str, *, txn_id: str | None = None) -> TurnResult:
        return TurnResult(
            status=status,
            text=text,
            actions=list(self._actions),
            txn_id=txn_id,
            usage=self._usage,
        )

    async def _emit_assistant_text(self, text: str) -> None:
        await self._link.send_event(
            "assistant_message",
            {"turn_id": self._turn_id, "message_id": self._message_id, "text": text},
        )

    def _failure_text(self, error: ProviderError) -> str:
        prefix = "The assistant could not reach the model."
        if self._actions:
            # Naming what already happened is the difference between "try again"
            # and "check your level", and the user cannot tell from the panel
            # alone whether an edit landed before the failure.
            prefix += " Some edits were already applied; review the level."
        return f"{prefix} {error}"

    def _summarise_silently(self, status: str) -> str:
        """A message for a turn that ended without the model saying anything."""
        if status == CANCELLED:
            return "Stopped."
        if self._actions:
            return f"Finished with {len(self._actions)} action(s)."
        if status == INCOMPLETE:
            return "The turn stopped before finishing. Nothing further was changed."
        return "Done."


def _describe(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> ActionRecord:
    """Build the user-facing summary line for one action (SPEC.md 8.9).

    Counts come from the *result* where the mod reports one, because that is what
    actually happened: a create of five objects where two ids were rejected should
    say two, not five.
    """
    if name == "create_objects":
        requested = arguments.get("objects") or []
        count = int(result.get("count", len(requested)) or 0)
        kinds = _object_kinds(requested)
        detail = f" ({kinds})" if kinds else ""
        # Say where they went. Without a position the summary is unactionable: the
        # objects are real and on the level, but the user has no way to find them,
        # which is indistinguishable from the edit having failed.
        where = _first_position(requested)
        return ActionRecord(
            kind=name, count=count, summary=f"Created {count} object(s){detail} at {where}"
        )

    if name == "modify_objects":
        count = int(result.get("modified_count", len(arguments.get("targets") or [])) or 0)
        changed = sorted(set(arguments.get("set") or {}) | set(arguments.get("delta") or {}))
        detail = f" ({', '.join(changed)})" if changed else ""
        return ActionRecord(kind=name, count=count, summary=f"Modified {count} object(s){detail}")

    if name == "delete_objects":
        count = int(result.get("deleted_count", len(arguments.get("targets") or [])) or 0)
        return ActionRecord(kind=name, count=count, summary=f"Deleted {count} object(s)")

    count = int(result.get("selected_count", len(arguments.get("targets") or [])) or 0)
    return ActionRecord(kind=name, count=count, summary=f"Selected {count} object(s)")


def _object_kinds(objects: list[Any]) -> str:
    """A short description of the object types created, e.g. `id 8`."""
    ids = [entry.get("id") for entry in objects if isinstance(entry, dict)]
    unique = sorted({entry for entry in ids if isinstance(entry, int)})
    if not unique:
        return ""
    if len(unique) <= 3:
        return ", ".join(f"id {value}" for value in unique)
    return f"{len(unique)} types"


def _first_position(objects: list[Any]) -> str:
    """Where the batch starts, as `x, y`, in the level's own coordinates.

    Reported because a count alone cannot be acted on: the user needs to know where
    to look, and the assistant cannot see the screen to tell them.
    """
    for entry in objects:
        if isinstance(entry, dict):
            x, y = entry.get("x"), entry.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                return f"x={x:g}, y={y:g}"
    return "an unknown position"


def _dump_result(result: dict[str, Any]) -> str:
    """Render a tool result for the model.

    JSON rather than prose: the model reads structured data more reliably, and
    every field it needs is already named. `_limit_reached` is stripped -- it is
    a signal to the loop, not information about the level.
    """
    return json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, default=str)
