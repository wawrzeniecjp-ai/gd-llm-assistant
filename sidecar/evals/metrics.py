"""What a run is scored on, and the assertions over it (LEVEL-DESIGN-SPEC.md 14, 15.4).

Metrics are measured on the level as the run left it (the fake editor), on the agent's
results, and on the tool calls the recording saw. An assertion names a metric and a
condition: a plain value must be equal; `{min}`, `{max}` or `{between = [lo, hi]}` bound a
number. A metric the run could not measure (no section to verify, say) fails its
assertion as `n/a`, so a missing measurement never passes silently.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from gd_ai_assistant import tools as tools_module
from gd_ai_assistant.agent import TurnResult
from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.music import alignment
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import CubeParams
from gd_ai_assistant.design.sim.verify import verify_section
from gd_ai_assistant.design.timeline import Timeline

from .cases import Case
from .fake_editor import FakeEditor
from .recording import ToolEvent

#: Tools that check playability. `place_grid` counts only with `verify`.
_VERIFIERS = {"verify_section", "playtest_verify"}


def _mutates(event: ToolEvent) -> bool:
    tool = tools_module.get(event.name)
    if tool is None or not tool.mutates_objects:
        return False
    return not (event.name == "place_grid" and event.arguments.get("preview"))


def _verifies(event: ToolEvent) -> bool:
    return event.name in _VERIFIERS or (event.name == "place_grid"
                                        and bool(event.arguments.get("verify")))


def measure(case: Case, editor: FakeEditor, before: dict[int, tuple], results: list[TurnResult],
            events: list[ToolEvent], elapsed_s: float,
            plan: dict[str, Any] | None = None) -> dict[str, Any]:
    roles = load_role_table()
    after = {uid: (o.type_id, o.props["x"], o.props["y"]) for uid, o in editor.objects.items()}
    created = [after[uid] for uid in after.keys() - before.keys()]
    deleted = before.keys() - after.keys()
    moved = sum(1 for uid in after.keys() & before.keys() if after[uid] != before[uid])
    created_roles = Counter()
    for type_id, _, _ in created:
        entry = roles.get(type_id)
        created_roles[entry.role.value if entry else "unknown"] += 1

    last_mutation = max((i for i, e in enumerate(events) if _mutates(e)), default=None)
    verified_after = last_mutation is None or any(
        _verifies(e) for e in events[last_mutation:])

    metrics: dict[str, Any] = {
        "status": "completed" if all(r.status == "completed" for r in results)
                  else next(r.status for r in results if r.status != "completed"),
        "tool_rounds": sum(r.usage.tool_rounds for r in results),
        "input_tokens": sum(r.usage.input_tokens for r in results),
        "output_tokens": sum(r.usage.output_tokens for r in results),
        "elapsed_s": round(elapsed_s, 1),
        "tool_calls": len(events),
        "tool_errors": sum(1 for e in events if e.error),
        "tools_used": dict(Counter(e.name for e in events)),
        "screenshots": sum(1 for e in events if e.name == "capture_screenshot"),
        "objects_created": len(created),
        "objects_deleted": len(deleted),
        "objects_moved": moved,
        "edited": bool(created or deleted or moved),
        "created_roles": dict(created_roles),
        "verified_after_last_edit": verified_after,
        "colors_set": sum(1 for e in events
                          if e.name == "set_color_channel" and not e.error),
    }
    metrics["plan_written"] = any(e.name == "update_level_plan" and not e.error
                                  for e in events)
    for name in ("verify_section", "simulate_inputs", "place_grid", "render_grid",
                 "beat_grid", "get_level_plan"):
        metrics[f"used_{name}"] = name in metrics["tools_used"]

    if case.section is not None:
        start_speed = editor.settings.get("start_speed", "1x")
        model = editor.level_model()
        check = verify_section(model, {"x_range": list(case.section), "budget_s": 20.0,
                                       "_level_start_speed": start_speed})
        metrics["playable"] = check.get("result")
        difficulty = check.get("difficulty") or {}
        for key in ("min_window_ms", "inputs_per_second", "frame_perfect"):
            if difficulty.get(key) is not None:
                metrics[key] = difficulty[key]
        music_plan = (plan or {}).get("music") or {}
        presses = [r["x"] for r in check.get("route") or []]
        if music_plan.get("bpm") and presses:
            timeline = Timeline.from_model(
                model, CubeParams.from_constants(load_constants()), start_speed,
                float(editor.settings.get("song_offset", 0.0)))
            aligned = alignment(music_plan, timeline, presses)
            metrics["beat_alignment"] = aligned["fraction"]
            metrics["beat_offsets_ms"] = aligned["offsets_ms"]
    return metrics


def lookup(metrics: dict[str, Any], name: str) -> Any:
    """`created_roles.orb` reaches into a nested count (absent counts as 0)."""
    value: Any = metrics
    for part in name.split("."):
        if not isinstance(value, dict):
            return None
        if part not in value:
            return 0 if name.startswith(("created_roles.", "tools_used.")) else None
        value = value[part]
    return value


def check(metrics: dict[str, Any], name: str, condition: Any) -> tuple[str, Any]:
    """('pass' | 'fail' | 'n/a', the measured value)."""
    value = lookup(metrics, name)
    if value is None:
        return "n/a", None
    if isinstance(condition, dict):
        if not isinstance(value, int | float) or isinstance(value, bool):
            return "fail", value
        lo = condition.get("min", (condition.get("between") or [None, None])[0])
        hi = condition.get("max", (condition.get("between") or [None, None])[1])
        ok = (lo is None or value >= lo) and (hi is None or value <= hi)
        return ("pass" if ok else "fail"), value
    return ("pass" if value == condition else "fail"), value
