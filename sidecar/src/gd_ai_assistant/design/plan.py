"""The level plan: the durable design record (LEVEL-DESIGN-SPEC.md 9).

One JSON file per level under `<data_dir>/plans/<sha256(level key)[:16]>.json`, written
atomically with the previous version kept as `.bak`. Never stored in the level. The plan
is design metadata, not level content, so it is not part of a turn's undo.

`x_range` is derived from a section's `start_ms`/`end_ms` through the timeline whenever the
plan is read: if a speed portal moves, the sections follow the music.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .constants import SPEEDS
from .level_model import LevelModel
from .roles import Role
from .timeline import Timeline

STATUSES = ["todo", "in_progress", "built", "verified", "needs_fix"]
GAMEPLAY_ROLES = (Role.SOLID, Role.SLOPE, Role.HAZARD, Role.ORB, Role.PAD,
                  Role.PORTAL_MODE, Role.PORTAL_SPEED, Role.PORTAL_GRAVITY, Role.PORTAL_SIZE)

_STATUS = {"type": "string", "enum": STATUSES}
_SECTION = {
    "type": "object",
    "required": ["id", "name"],
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 32},
        "name": {"type": "string", "maxLength": 80},
        "start_ms": {"type": "number", "minimum": 0},
        "end_ms": {"type": "number", "minimum": 0},
        "x_range": {"type": "array", "items": {"type": "number"}, "minItems": 2,
                    "maxItems": 2},
        "mode": {"type": "string", "enum": ["cube", "ship", "ball", "ufo", "wave", "robot",
                                            "spider", "swing"]},
        "speed": {"type": "string", "enum": list(SPEEDS)},
        "gravity": {"type": "integer", "enum": [1, -1]},
        "mini": {"type": "boolean"},
        "difficulty": {"type": "integer", "minimum": 1, "maximum": 10},
        "intensity": {"type": "number", "minimum": 0, "maximum": 1},
        "style": {"type": "string"},
        "status": {"type": "object", "properties": {
            "gameplay": _STATUS, "deco": _STATUS, "effects": _STATUS},
            "additionalProperties": False},
        "verified": {"type": "object"},
        "notes": {"type": "string", "maxLength": 2000},
    },
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "required": ["schema", "sections"],
    "properties": {
        "schema": {"const": 1},
        "level_key": {"type": "string"},
        "level_name": {"type": "string"},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
        "concept": {"type": "string", "maxLength": 2000},
        "target_difficulty": {"type": "string", "maxLength": 40},
        "music": {
            "type": "object",
            "properties": {
                "bpm": {"type": ["number", "null"], "exclusiveMinimum": 0,
                        "maximum": 1000},
                "offset_ms": {"type": "number"},
                "beats_per_bar": {"type": "integer", "minimum": 1, "maximum": 16},
                "source": {"type": ["string", "null"],
                           "enum": ["user", "guidelines", "plan", "analysis", None]},
                "tempo_changes": {"type": "array", "items": {
                    "type": "object", "required": ["at_ms", "bpm"],
                    "properties": {"at_ms": {"type": "number", "minimum": 0},
                                   "bpm": {"type": "number", "exclusiveMinimum": 0}},
                    "additionalProperties": False}},
                "sections": {"type": "array"},
            },
            "additionalProperties": False,
        },
        "allow_replay": {"type": "boolean"},
        "styles": {"type": "object"},
        "sections": {"type": "array", "items": _SECTION, "maxItems": 200},
        "decisions": {"type": "array", "items": {
            "type": "object", "required": ["text"],
            "properties": {"at": {"type": "string"},
                           "text": {"type": "string", "maxLength": 500}},
            "additionalProperties": False}},
        "todo": {"type": "array", "items": {"type": "string", "maxLength": 300}},
    },
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(SCHEMA)


class PlanError(ValueError):
    """An op the plan refuses: the batch is rejected and nothing is written."""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def default_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "gd-ai-assistant"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "gd-ai-assistant"


def new_plan(level_key: str, level_name: str) -> dict[str, Any]:
    now = _now()
    return {"schema": 1, "level_key": level_key, "level_name": level_name,
            "created_at": now, "updated_at": now, "concept": "", "target_difficulty": "",
            "music": {"bpm": None, "offset_ms": 0, "beats_per_bar": 4, "source": None,
                      "tempo_changes": [], "sections": []},
            "allow_replay": False, "styles": {}, "sections": [], "decisions": [],
            "todo": []}


class PlanStore:
    """Plans on disk, keyed by the level key the mod reports."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.root = Path(data_dir or default_data_dir()) / "plans"

    def path(self, level_key: str) -> Path:
        digest = hashlib.sha256(level_key.encode("utf-8")).hexdigest()[:16]
        return self.root / f"{digest}.json"

    def load(self, level_key: str) -> dict[str, Any] | None:
        path = self.path(level_key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, plan: dict[str, Any]) -> None:
        problems = [e.message for e in _VALIDATOR.iter_errors(plan)]
        if problems:
            raise PlanError("; ".join(problems[:5]))
        path = self.path(plan["level_key"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.replace(path.with_suffix(".json.bak"))
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, indent=1)
        os.replace(tmp, path)


# -- ops -----------------------------------------------------------------------------


def _section(plan: dict[str, Any], section_id: str) -> dict[str, Any]:
    for section in plan["sections"]:
        if section["id"] == section_id:
            return section
    raise PlanError(f"no section {section_id!r}")


def _set_path(plan: dict[str, Any], path: str, value: Any) -> None:
    parts = [p for p in path.split(".") if p]
    if not parts or parts[0] in ("schema", "level_key", "created_at", "updated_at",
                                 "sections", "decisions"):
        raise PlanError(f"set_field cannot change {path!r}; use the section and decision ops")
    target = plan
    for part in parts[:-1]:
        if not isinstance(target.get(part), dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def apply_ops(plan: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """The plan after `ops`, or PlanError; `plan` itself is not changed."""
    if not isinstance(ops, list) or not ops:
        raise PlanError("give `ops` as a non-empty list")
    out = copy.deepcopy(plan)
    for i, op in enumerate(ops):
        kind = op.get("op") if isinstance(op, dict) else None
        try:
            if kind == "set_field":
                _set_path(out, str(op["path"]), op.get("value"))
            elif kind == "add_section":
                section = dict(op["section"])
                if any(s["id"] == section.get("id") for s in out["sections"]):
                    raise PlanError(f"a section {section.get('id')!r} already exists")
                section.setdefault("status", {"gameplay": "todo", "deco": "todo",
                                              "effects": "todo"})
                after = op.get("after")
                index = (len(out["sections"]) if after is None else
                         out["sections"].index(_section(out, after)) + 1)
                out["sections"].insert(index, section)
            elif kind == "update_section":
                section = _section(out, op["id"])
                fields = dict(op["fields"])
                if "id" in fields and fields["id"] != section["id"]:
                    raise PlanError("a section's id cannot change")
                status = fields.pop("status", None)
                section.update(fields)
                if status is not None:
                    section.setdefault("status", {}).update(status)
            elif kind == "remove_section":
                out["sections"].remove(_section(out, op["id"]))
            elif kind == "add_decision":
                out["decisions"].append({"at": _now(), "text": str(op["text"])})
            elif kind == "set_music":
                music = {k: v for k, v in op.items() if k != "op"}
                out["music"].update(music)
            elif kind == "set_style":
                out["styles"][str(op["name"])] = op["profile"]
            else:
                raise PlanError(f"unknown op {kind!r}")
        except KeyError as exc:
            raise PlanError(f"op {i} ({kind}): missing {exc.args[0]!r}") from exc
        except PlanError as exc:
            raise PlanError(f"op {i} ({kind}): {exc}") from exc
    problems = [f"{'/'.join(map(str, e.path)) or 'plan'}: {e.message}"
                for e in _VALIDATOR.iter_errors(out)]
    if problems:
        raise PlanError("the plan would be invalid, nothing was changed: "
                        + "; ".join(problems[:5]))
    for section in out["sections"]:
        if "start_ms" in section and "end_ms" in section \
                and section["end_ms"] <= section["start_ms"]:
            raise PlanError(f"section {section['id']!r}: end_ms must be after start_ms")
    out["updated_at"] = _now()
    return out


# -- reading -------------------------------------------------------------------------


def gameplay_hash(model: LevelModel, x_range: tuple[float, float]) -> str:
    """The section's gameplay objects, as a short hash: what `verified` is checked by."""
    x0, x1 = sorted(x_range)
    objects = sorted((o.uid, o.type_id, round(o.x, 1), round(o.y, 1))
                     for o in model.in_rect(x0, -1e9, x1, 1e9, roles=GAMEPLAY_ROLES))
    return hashlib.sha256(repr(objects).encode()).hexdigest()[:16]


def section_x_range(section: dict[str, Any], timeline: Timeline | None
                    ) -> tuple[float, float] | None:
    if timeline is not None and "start_ms" in section and "end_ms" in section:
        return (round(timeline.x_at_song_ms(section["start_ms"]), 1),
                round(timeline.x_at_song_ms(section["end_ms"]), 1))
    if section.get("x_range"):
        return tuple(section["x_range"])  # type: ignore[return-value]
    return None


def with_derived(plan: dict[str, Any], timeline: Timeline | None,
                 model: LevelModel | None, level_name: str | None) -> dict[str, Any]:
    """The plan as `get_level_plan` shows it: derived x ranges, staleness, progress."""
    out = copy.deepcopy(plan)
    stale = []
    for section in out["sections"]:
        derived = section_x_range(section, timeline)
        if derived is not None:
            section["x_range"] = list(derived)
        verified = section.get("verified") or {}
        if model is not None and derived and verified.get("hash") \
                and gameplay_hash(model, derived) != verified["hash"]:
            section["stale"] = True
            stale.append(section["id"])
    done = sum(1 for s in out["sections"]
               if (s.get("status") or {}).get("gameplay") == "verified" and not s.get("stale"))
    out["progress"] = {"sections": len(out["sections"]), "gameplay_verified": done,
                       "stale": stale}
    if level_name is not None and plan.get("level_name") not in (None, "", level_name):
        out["warning"] = (f"this plan was made for a level named {plan['level_name']!r}; "
                          f"the open level is {level_name!r}")
    return out


def render_block(plan: dict[str, Any], max_chars: int = 2400) -> str:
    """The plan for the system prompt (9.5): about 600 tokens at most."""
    lines = ["## Level plan", "(design notes for this level; get_level_plan has all of it)"]
    if plan.get("concept"):
        lines.append(f"Concept: {plan['concept']}")
    if plan.get("target_difficulty"):
        lines.append(f"Target difficulty: {plan['target_difficulty']}")
    music = plan.get("music") or {}
    if music.get("bpm"):
        lines.append(f"Music: {music['bpm']} bpm, first beat at {music.get('offset_ms', 0)} ms "
                     f"(song time), {music.get('beats_per_bar', 4)} beats per bar, "
                     f"source {music.get('source')}")
    else:
        lines.append("Music: bpm unknown -- ask the user before syncing to the beat")
    if plan.get("sections"):
        lines.append("Sections (id | name | ms | mode speed | difficulty | gameplay/deco):")
        for s in plan["sections"]:
            status = s.get("status") or {}
            span = (f"{s.get('start_ms', '?')}-{s.get('end_ms', '?')}"
                    if "start_ms" in s else str(s.get("x_range", "?")))
            stale = " STALE" if s.get("stale") else ""
            lines.append(f"- {s['id']} | {s.get('name', '')} | {span} | "
                         f"{s.get('mode', 'cube')} {s.get('speed', '?')} | "
                         f"{s.get('difficulty', '?')} | {status.get('gameplay', 'todo')}/"
                         f"{status.get('deco', 'todo')}{stale}")
    decisions = plan.get("decisions") or []
    if decisions:
        lines.append("Recent decisions:")
        lines += [f"- {d['text']}" for d in decisions[-5:]]
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars - 20] + "\n(... truncated)"
