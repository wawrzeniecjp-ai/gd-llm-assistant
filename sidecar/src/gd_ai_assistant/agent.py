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
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import catalog as catalog_module
from . import tools
from .agent_prompt import system_prompt
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

#: Cap on pages fetched in one go. A guard against a mod that never reports the last
#: page, which would otherwise loop until the turn's wall-clock timeout.
_MAX_CATALOG_PAGES = 40


class EditorLink(Protocol):
    """What the loop needs from the connection to the mod."""

    async def request(self, type_name: str, payload: dict[str, Any] | None = None) -> Any:
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
                    system=system_prompt(), messages=messages, tools=tools.tool_specs()
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

    async def _execute(self, call: ToolCall) -> tuple[dict[str, Any], bool]:
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

        if tool.protocol_type is None:
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

        response = await self._link.request(tool.protocol_type, call.arguments)
        if not response.ok:
            error = response.error or {}
            return (
                {
                    "error": error.get("message", "the request failed"),
                    "code": error.get("code", "INTERNAL_ERROR"),
                },
                True,
            )

        result = response.result if isinstance(response.result, dict) else {}
        if tool.mutates_objects:
            self._objects_used += tool.object_count(call.arguments)
        if tool.is_action:
            self._actions.append(_describe(tool.spec.name, call.arguments, result))
        return result, False

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
