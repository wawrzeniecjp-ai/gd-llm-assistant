"""Scripted LLM provider (SPEC.md 13).

Produces a deterministic turn from the user's message with no network, no API
key and no cost. This is what makes the Phase 3 exit criterion -- "panel works
end-to-end against the fake sidecar" -- meaningful: the panel can be exercised
against real protocol frames without the LLM tool loop (Phase 4) existing yet,
and the same determinism is what cancellation and error-path tests need.

Determinism is the whole point, so nothing here may depend on process state:

* ``hash()`` is **not** used to derive anything. Python randomises string hashing
  per process (PYTHONHASHSEED), so the same message would produce a different
  turn on every run and any test asserting on it would be flaky in a way that
  looks like a real bug.
* ``zlib.crc32`` is used instead: cheap, stable across processes and versions,
  and good enough to vary the reply between messages. Nothing depends on it
  being a good hash -- it only stops every reply looking identical, so a round
  trip that silently reuses a stale message is visible.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlannedAction:
    """One entry of a `turn_finished.actions` array (SPEC.md 7.4)."""

    kind: str
    count: int
    summary: str

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "count": self.count, "summary": self.summary}


@dataclass(frozen=True)
class PlannedTurn:
    """The text and actions a scripted turn reports."""

    text: str
    actions: tuple[PlannedAction, ...]


def _seed(text: str) -> int:
    """A stable non-negative integer derived from `text`."""
    return zlib.crc32(text.encode("utf-8"))


def plan_turn(user_text: str) -> PlannedTurn:
    """Build the reply for `user_text`.

    The reply deliberately contains things the mod is supposed to handle rather
    than only things it renders happily:

    * multiple lines and a fenced block, to exercise wrapping and layout;
    * GD colour tags (``<cr>``, ``<c-FF00FF>``, the closing ``</c>``), which are
      untrusted markup and must be *stripped* by the mod's sanitiser rather than
      rendered (SPEC.md 8.3.1). Seeing them as literal text in the panel means
      the sanitiser is missing;
    * the user's own text echoed back, which is where an injection would arrive.
    """
    seed = _seed(user_text)
    count = 3 + seed % 3  # 3..5
    layer = seed % 4
    blocks = ("spike", "block", "platform", "saw")

    text = (
        f"Scripted reply ({count} planned objects, layer {layer}).\n"
        f"You asked: {user_text}\n"
        f"<cr>This tag is stripped, not coloured.<c/>\n"
        "<c-FF00FF>Nor is this one.</c>\n"
        "\n"
        "```\n"
        f"create_objects {{ count: {count}, layer: {layer} }}\n"
        "```\n"
        "Nothing was actually changed: this provider does not touch the editor."
    )

    actions = (
        PlannedAction(
            kind="create_objects",
            count=count,
            summary=f"Created {count} {blocks[seed % len(blocks)]}s at layer {layer}",
        ),
        PlannedAction(
            kind="modify_objects",
            count=2,
            summary="Modified 2 objects (colour channel 1 -> 2)",
        ),
    )

    return PlannedTurn(text=text, actions=actions)
