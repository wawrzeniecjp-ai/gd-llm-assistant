"""An in-memory editor that speaks the mod's protocol (LEVEL-DESIGN-SPEC.md 15.2, LD17).

The agent loop talks to the mod through `EditorLink` (`agent.py`). This class
implements that interface over an in-memory level, so a complete turn -- tool calls,
transactions, snapshots -- runs without Geometry Dash.

**It cannot drift from the protocol.** Every request payload is validated against the
schema registered for its type before it is handled, and every result against the
result schema before it is returned. A fake that accepted what the mod would refuse,
or answered in a shape the mod never sends, would make tests pass for the wrong
reason; here either mistake fails loudly with the schema error.

Requests without a registered schema (`list_object_types`) are answered in the shape
the mod's job produces (`src/exec/editor_jobs.cpp`).

What it does **not** model: the game's rendering (`capture_screenshot` answers with a
schematic of the camera's view, and says so), physics, and anything the role table does
not know.
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from gd_ai_assistant import protocol

#: Request types answered without a registered schema, in the mod's own shape.
_UNSCHEMED = frozenset({"list_object_types"})

#: Settable named properties and their defaults (SPEC.md 7.5).
_DEFAULTS: dict[str, Any] = {
    "rotation": 0.0,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "flip_x": False,
    "flip_y": False,
    "z_layer": "default",
    "z_order": 0,
    "editor_layer": 0,
    "groups": [],
}
_DELTA_PROPERTIES = ("x", "y", "rotation")


@dataclass
class FakeResponse:
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @property
    def code(self) -> str | None:
        return None if self.error is None else self.error.get("code")


@dataclass
class FakeObject:
    uid: int
    type_id: int
    props: dict[str, Any]
    #: Save-string keys beyond the named properties, as text.
    extra: dict[int, str] = field(default_factory=dict)

    def save_string(self) -> str:
        parts = [f"1,{self.type_id}", f"2,{_fmt(self.props['x'])}", f"3,{_fmt(self.props['y'])}"]
        parts.extend(f"{key},{value}" for key, value in sorted(self.extra.items()))
        return ",".join(parts)

    def property_set(self) -> dict[str, Any]:
        out = {"x": self.props["x"], "y": self.props["y"]}
        for name, default in _DEFAULTS.items():
            out[name] = copy.deepcopy(self.props.get(name, default))
        for name in ("color_channel_1", "color_channel_2"):
            if name in self.props:
                out[name] = self.props[name]
        return out

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "object_id": self.uid,
            "id": self.type_id,
            "x": self.props["x"],
            "y": self.props["y"],
        }
        if self.props.get("rotation"):
            out["rotation"] = self.props["rotation"]
        return out


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


class _Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Transaction:
    txn_id: str
    before: dict[int, FakeObject]
    created: int = 0
    modified: int = 0
    deleted: int = 0


class FakeEditor:
    """`EditorLink` over an in-memory level."""

    def __init__(
        self,
        *,
        describe: dict[int, dict[str, Any]] | None = None,
        catalog: dict[int, str | None] | None = None,
        unsupported: set[str] | None = None,
        level_name: str = "Fake level",
    ) -> None:
        self.objects: dict[int, FakeObject] = {}
        self.revision = 0
        self.level_name = level_name
        self.selection: set[int] = set()
        self.colors: dict[int, tuple[int, int, int]] = {}
        self.settings: dict[str, Any] = {"background": 0, "ground": 0, "start_mode": 0}
        self.camera = {"x": 0.0, "y": 0.0, "zoom": 1.0}
        #: `describe_object_types` answers, keyed by id; from a roles fixture.
        self.describe = dict(describe or {})
        #: id -> frame, for `list_object_catalog`.
        self.catalog = dict(catalog or {})
        #: Request types this fake pretends not to implement (an older mod).
        self.unsupported = set(unsupported or ())
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        #: Called before each request is handled; lets a test edit "by hand" mid-read.
        self.before_request: Callable[[str, dict[str, Any]], None] | None = None
        self._uids = itertools.count(1)
        self._txn: _Transaction | None = None
        self._txn_counter = itertools.count(1)
        self._last_txn: _Transaction | None = None
        self._validators: dict[str, Draft202012Validator] = {}
        self._captures = itertools.count(1)

    # -- EditorLink -----------------------------------------------------------

    async def request(self, type_name: str, payload: dict[str, Any] | None = None,
                      timeout: float | None = None) -> FakeResponse:
        payload = dict(payload or {})
        self.requests.append((type_name, payload))
        if self.before_request is not None:
            self.before_request(type_name, payload)

        if type_name in self.unsupported:
            return self._error("UNSUPPORTED_REQUEST", f"'{type_name}' is not implemented")

        spec = protocol.spec_for(type_name)
        if spec is None and type_name not in _UNSCHEMED:
            return self._error("UNSUPPORTED_REQUEST", f"'{type_name}' is not implemented")
        if spec is not None:
            problems = self._validate(spec.payload_ref, payload)
            if problems:
                return self._error("INVALID_REQUEST", "; ".join(problems))

        handler = getattr(self, f"_handle_{type_name}", None)
        if handler is None:
            return self._error("UNSUPPORTED_REQUEST", f"'{type_name}' is not implemented")
        try:
            result = handler(payload)
        except _Refused as refused:
            return self._error(refused.code, refused.message)

        if spec is not None and spec.result_ref is not None:
            problems = self._validate(spec.result_ref, result)
            if problems:
                raise AssertionError(
                    f"fake editor produced an invalid {type_name} result: {problems}"
                )
        return FakeResponse(ok=True, result=result)

    async def send_event(self, type_name: str, payload: dict[str, Any]) -> None:
        self.events.append((type_name, payload))

    # -- test helpers -----------------------------------------------------------

    def add(self, type_id: int, x: float, y: float, **props: Any) -> int:
        """Place an object directly, as if it had been in the level all along."""
        uid = next(self._uids)
        extra = props.pop("extra", {})
        self.objects[uid] = FakeObject(uid, type_id, {"x": float(x), "y": float(y), **props},
                                       dict(extra))
        self.revision += 1
        return uid

    def hand_edit(self) -> None:
        """Simulate the user changing the level: moves the revision like the mod's poll."""
        self.revision += 1

    # -- validation ---------------------------------------------------------------

    def _validate(self, ref: str, instance: Any) -> list[str]:
        validator = self._validators.get(ref)
        if validator is None:
            _, _, registry, _ = protocol._load(protocol.PROTOCOL_VERSION)
            validator = Draft202012Validator({"$ref": ref}, registry=registry)
            self._validators[ref] = validator
        return [error.message for error in validator.iter_errors(instance)]

    @staticmethod
    def _error(code: str, message: str) -> FakeResponse:
        return FakeResponse(ok=False, error={"code": code, "message": message})

    def _mutated(self) -> None:
        self.revision += 1

    def _resolve_all(self, targets: list[int]) -> tuple[list[FakeObject], list[int]]:
        found = [self.objects[t] for t in targets if t in self.objects]
        missing = [t for t in targets if t not in self.objects]
        if not found:
            raise _Refused("OBJECT_NOT_FOUND", "No matching objects.")
        return found, missing

    # -- reads ------------------------------------------------------------------------

    def _handle_ping(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}

    def _handle_get_editor_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {
            "object_count": len(self.objects),
            "selected": {"count": len(self.selection)},
            "editor_layer": 0,
            "grid_size": 30,
            "playtesting": False,
            "level": {"name": self.level_name},
            "revision": self.revision,
        }
        state["camera"] = self._camera()
        return state

    def _matches(self, obj: FakeObject, filters: dict[str, Any]) -> bool:
        if "id" in filters and obj.type_id != filters["id"]:
            return False
        if "ids" in filters and obj.uid not in filters["ids"]:
            return False
        if "group" in filters and filters["group"] not in obj.props.get("groups", []):
            return False
        layer = obj.props.get("editor_layer", 0)
        if "editor_layer" in filters and layer != filters["editor_layer"]:
            return False
        if filters.get("selected_only") and obj.uid not in self.selection:
            return False
        for axis, key in (("x", "x_range"), ("y", "y_range")):
            if key in filters:
                low, high = filters[key]
                if not low <= obj.props[axis] <= high:
                    return False
        return True

    def _handle_query_objects(self, payload: dict[str, Any]) -> dict[str, Any]:
        filters = payload.get("filters") or {}
        limit = payload.get("limit", 100)
        offset = int(payload.get("cursor") or 0)
        matches = [o for o in self._ordered() if self._matches(o, filters)]
        page = matches[offset:offset + limit]
        more = offset + limit < len(matches)
        return {
            "objects": [o.record() for o in page],
            "total_matches": len(matches),
            "truncated": more,
            "next_cursor": str(offset + limit) if more else None,
        }

    def _handle_summarize_region(self, payload: dict[str, Any]) -> dict[str, Any]:
        matches = [o for o in self._ordered() if self._matches(o, payload)]
        counts: dict[int, int] = {}
        for obj in matches:
            counts[obj.type_id] = counts.get(obj.type_id, 0) + 1
        result: dict[str, Any] = {
            "total": len(matches),
            "by_type": [
                {"id": type_id, "count": count}
                for type_id, count in sorted(counts.items(), key=lambda kv: -kv[1])[:20]
            ],
        }
        if matches:
            result["bounds"] = {
                "x1": min(o.props["x"] for o in matches),
                "y1": min(o.props["y"] for o in matches),
                "x2": max(o.props["x"] for o in matches),
                "y2": max(o.props["y"] for o in matches),
            }
        return result

    def _handle_list_object_types(self, payload: dict[str, Any]) -> dict[str, Any]:
        types: dict[int, dict[str, Any]] = {}
        for obj in self._ordered():
            entry = types.setdefault(obj.type_id, {
                "id": obj.type_id, "count": 0, "width": 30.0, "height": 30.0,
                "sample_x": obj.props["x"], "sample_y": obj.props["y"],
            })
            entry["count"] += 1
        result: dict[str, Any] = {"total_objects": len(self.objects), "types": list(types.values())}
        if self.objects:
            result["lowest_y"] = min(o.props["y"] for o in self.objects.values())
        return result

    def _handle_get_object_properties(self, payload: dict[str, Any]) -> dict[str, Any]:
        obj = self.objects.get(payload["object_id"])
        if obj is None:
            raise _Refused("OBJECT_NOT_FOUND", "No matching objects.")
        return {"properties": obj.property_set()}

    def _handle_run_replay(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The replay played by the simulator itself, in the mod's tick numbering.

        Physics here is the simulator's, so this checks the plumbing of a playtest (start,
        inputs, trace, outcome), never the physics: that is what the real game is for.
        """
        from gd_ai_assistant.design.constants import load_constants
        from gd_ai_assistant.design.roles import load_role_table
        from gd_ai_assistant.design.sim.cube import Cube, CubeParams, advance
        from gd_ai_assistant.design.sim.world import world_from_objects

        if self._txn is not None:
            raise _Refused("EDITOR_BUSY", "a transaction is open; end it before running a replay")
        params = CubeParams.from_constants(load_constants())
        objects = [{"id": o.type_id, "x": o.props["x"], "y": o.props["y"]}
                   for o in self.objects.values()]
        world = world_from_objects(objects, load_role_table())
        start = payload.get("start") or {"kind": "level_start"}
        cube, started = Cube(), {"kind": "level_start"}
        if start["kind"] == "start_pos":
            obj = self.objects.get(start["uid"])
            if obj is None or obj.type_id != 31:
                raise _Refused("NOT_FOUND", f"no start position with uid {start['uid']}")
            cube = Cube(x=obj.props["x"], y=obj.props["y"])
            started = {"kind": "start_pos", "uid": obj.uid, "x": obj.props["x"],
                       "y": obj.props["y"]}
        by_tick: dict[int, list[str]] = {}
        for item in payload.get("inputs", []):
            by_tick.setdefault(item["tick"], []).append(item["action"])
        trace: dict[str, list[Any]] = {"tick": [], "x": [], "y": []}

        def sample(t: int) -> None:
            trace["tick"].append(t)
            trace["x"].append(round(cube.x, 3))
            trace["y"].append(round(cube.y, 3))

        sample(0)
        sample(1)                                  # the level start's standing tick
        outcome, death = "max_ticks", None
        for t in range(2, payload.get("max_ticks", 2400) + 1):
            died = advance(cube, world, params, t, by_tick.get(t - 1, ()))
            sample(t)
            if died is not None:
                outcome = "death"
                death = {"tick": t, "x": died.x, "y": died.y, "cause_uid": None,
                         "cause_id": None}
                break
        return {"outcome": outcome, "ticks": trace["tick"][-1], "death": death,
                "trace": trace, "tick_rate_observed": 240.0, "start": started}

    def _handle_get_level_objects(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = payload.get("limit", 1000)
        offset = 0
        cursor = payload.get("cursor")
        if cursor is not None:
            offset_text, _, revision_text = cursor.partition("@")
            if not offset_text.isdigit() or not revision_text.isdigit():
                raise _Refused("INVALID_REQUEST", "'cursor' is not one this mod issued")
            if int(revision_text) != self.revision:
                raise _Refused(
                    "LEVEL_CHANGING",
                    "The level changed since the first page; start again without a cursor.",
                )
            offset = int(offset_text)
        ordered = self._ordered()
        end = min(len(ordered), offset + limit)
        return {
            "objects": [
                {
                    "object_id": o.uid,
                    "id": o.type_id,
                    "properties": o.property_set(),
                    "save": o.save_string(),
                }
                for o in ordered[offset:end]
            ],
            "total": len(ordered),
            "revision": self.revision,
            "next_cursor": f"{end}@{self.revision}" if end < len(ordered) else None,
        }

    def _handle_describe_object_types(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "ids" in payload:
            ids = list(payload["ids"])
            total, next_offset = len(ids), None
        else:
            known = sorted(set(self.describe) | set(self.catalog))
            total = len(known)
            offset = payload.get("offset", 0)
            limit = payload.get("limit", 200)
            ids = known[offset:offset + limit]
            next_offset = offset + limit if offset + limit < total else None
        entries = [copy.deepcopy(self.describe[i]) for i in ids if i in self.describe]
        failed = [i for i in ids if i not in self.describe]
        return {"entries": entries, "total": total, "next_offset": next_offset, "failed": failed}

    def _handle_list_object_catalog(self, payload: dict[str, Any]) -> dict[str, Any]:
        ids = sorted(self.catalog)
        offset = payload.get("offset", 0)
        limit = payload.get("limit", 200)
        page = ids[offset:offset + limit]
        entries: list[dict[str, Any]] = []
        for object_id in page:
            entry: dict[str, Any] = {"id": object_id}
            frame = self.catalog[object_id]
            if frame:
                entry["frame"] = frame
            entries.append(entry)
        nxt = offset + limit
        next_offset = nxt if nxt < len(ids) else None
        return {"entries": entries, "total": len(ids), "next_offset": next_offset}

    def _handle_get_save_properties(self, payload: dict[str, Any]) -> dict[str, Any]:
        obj = self.objects.get(payload["object_id"])
        if obj is None:
            raise _Refused("OBJECT_NOT_FOUND", "No matching objects.")
        pairs = obj.save_string().split(",")
        props = [{"key": int(pairs[i]), "value": pairs[i + 1]} for i in range(0, len(pairs), 2)]
        return {"object_id": obj.uid, "properties": props, "count": len(props)}

    def _handle_get_color_channel(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel = payload["channel"]
        rgb = self.colors.get(channel)
        red, green, blue = rgb or (255, 255, 255)
        return {"channel": channel, "exists": rgb is not None, "red": red, "green": green,
                "blue": blue}

    def _handle_get_level_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"settings": copy.deepcopy(self.settings)}

    def _camera(self) -> dict[str, Any]:
        half_w = 285.0 / self.camera["zoom"]
        half_h = 160.0 / self.camera["zoom"]
        x, y = self.camera["x"], self.camera["y"]
        return {"x": x, "y": y, "zoom": self.camera["zoom"],
                "visible": {"x1": x - half_w, "y1": y - half_h, "x2": x + half_w, "y2": y + half_h}}

    def _handle_get_camera(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"camera": self._camera()}

    def level_model(self) -> Any:
        """The level as the design tools would read it."""
        from gd_ai_assistant.design.level_model import LevelModel, LevelObject
        from gd_ai_assistant.design.roles import load_role_table

        roles = load_role_table()
        records = self._handle_get_level_objects({"limit": 10**9})["objects"]
        return LevelModel([LevelObject.from_record(r, roles) for r in records],
                          revision=self.revision)

    def _handle_capture_screenshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A schematic of what the camera shows (LEVEL-DESIGN-SPEC.md 15.2), written to a
        PNG like the mod's capture. The path's name says it is a schematic; the agent's
        description of the capture passes the name on."""
        import tempfile
        from pathlib import Path

        from gd_ai_assistant.design.coords import Grid
        from gd_ai_assistant.design.render import SchematicError, render_schematic

        view = self._camera()["visible"]
        if payload.get("region") == "selection":
            chosen = [self.objects[uid] for uid in self.selection if uid in self.objects]
            if not chosen:
                raise _Refused("INVALID_REQUEST", "the selection holds no usable objects")
            pad = float(payload.get("radius", 60.0))
            xs = [o.props["x"] for o in chosen]
            ys = [o.props["y"] for o in chosen]
            view = {"x1": min(xs) - pad, "x2": max(xs) + pad,
                    "y1": min(ys) - pad, "y2": max(ys) + pad}
        try:
            schematic = render_schematic(
                self.level_model(), Grid.from_constants(),
                {"x_range": [view["x1"], view["x2"]],
                 "y_range": [max(view["y1"], 0.0), max(view["y2"], 90.0)],
                 "layers": ["gameplay", "deco"], "px_per_cell": 24})
        except SchematicError as exc:
            raise _Refused("INVALID_REQUEST", f"cannot draw this view: {exc}") from exc
        folder = Path(tempfile.gettempdir()) / "gd-ai-fake-captures"
        folder.mkdir(exist_ok=True)
        path = folder / f"schematic-{next(self._captures)}.png"
        path.write_bytes(schematic.png)
        targets = len(self.selection) if payload.get("region") == "selection" else 0
        return {"path": str(path), "width": schematic.width, "height": schematic.height,
                "targets": targets, "viewport": view}

    # -- writes -----------------------------------------------------------------------

    def _handle_set_camera(self, payload: dict[str, Any]) -> dict[str, Any]:
        for key in ("x", "y"):
            if key in payload:
                self.camera[key] = float(payload[key])
        if "zoom" in payload:
            self.camera["zoom"] = min(4.0, max(0.05, float(payload["zoom"])))
        return {"camera": self._camera()}

    def _handle_set_level_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = payload.get("settings", payload)
        self.settings.update(settings)
        return {"settings": copy.deepcopy(self.settings), "revertible": False}

    def _handle_set_color_channel(self, payload: dict[str, Any]) -> dict[str, Any]:
        channel = payload["channel"]
        current = self.colors.get(channel, (255, 255, 255))
        rgb = (payload.get("red", current[0]), payload.get("green", current[1]),
               payload.get("blue", current[2]))
        self.colors[channel] = rgb
        return {"channel": channel, "exists": True, "red": rgb[0], "green": rgb[1], "blue": rgb[2]}

    def _handle_set_selection(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.selection = {t for t in payload["targets"] if t in self.objects}
        return {"selected_count": len(self.selection)}

    def _handle_begin_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._txn is not None:
            raise _Refused("TRANSACTION_ACTIVE", "A transaction is already open.")
        self._txn = _Transaction(f"txn-{next(self._txn_counter)}", copy.deepcopy(self.objects))
        return {"txn_id": self._txn.txn_id}

    def _handle_end_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        txn = self._txn
        if txn is None or txn.txn_id != payload["txn_id"]:
            raise _Refused("INVALID_REQUEST", "No such open transaction.")
        self._txn = None
        self._last_txn = txn
        return {"txn_id": txn.txn_id,
                "summary": {"created": txn.created, "modified": txn.modified,
                            "deleted": txn.deleted}}

    def _handle_revert_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        txn = self._last_txn
        if txn is None or txn.txn_id != payload["txn_id"]:
            raise _Refused("TRANSACTION_NOT_REVERTIBLE", "Not the most recent change.")
        self.objects = txn.before
        self._last_txn = None
        self._mutated()
        return {"reverted": True}

    def _count(self, attr: str, n: int) -> None:
        if self._txn is not None:
            setattr(self._txn, attr, getattr(self._txn, attr) + n)

    def _handle_create_objects(self, payload: dict[str, Any]) -> dict[str, Any]:
        created: list[int] = []
        for spec in payload["objects"]:
            uid = next(self._uids)
            props = {"x": float(spec["x"]), "y": float(spec["y"]), **(spec.get("properties") or {})}
            self.objects[uid] = FakeObject(uid, spec["id"], props)
            created.append(uid)
        self._count("created", len(created))
        self._mutated()
        return {"created": created, "count": len(created)}

    def _handle_modify_objects(self, payload: dict[str, Any]) -> dict[str, Any]:
        found, missing = self._resolve_all(payload["targets"])
        for obj in found:
            obj.props.update(copy.deepcopy(payload.get("set") or {}))
            for name, delta in (payload.get("delta") or {}).items():
                if name in _DELTA_PROPERTIES:
                    obj.props[name] = float(obj.props.get(name, 0.0)) + float(delta)
        self._count("modified", len(found))
        self._mutated()
        return {"modified_count": len(found), "missing": missing}

    def _handle_delete_objects(self, payload: dict[str, Any]) -> dict[str, Any]:
        found, missing = self._resolve_all(payload["targets"])
        for obj in found:
            del self.objects[obj.uid]
            self.selection.discard(obj.uid)
        self._count("deleted", len(found))
        self._mutated()
        return {"deleted_count": len(found), "missing": missing}

    def _handle_set_save_property(self, payload: dict[str, Any]) -> dict[str, Any]:
        found, missing = self._resolve_all(payload["targets"])
        replacements = []
        for obj in found:
            del self.objects[obj.uid]
            new_uid = next(self._uids)
            obj.extra[int(payload["key"])] = str(payload["value"])
            self.objects[new_uid] = FakeObject(new_uid, obj.type_id, obj.props, obj.extra)
            replacements.append({"old_id": obj.uid, "new_id": new_uid})
        self._count("modified", len(found))
        self._mutated()
        return {"modified_count": len(found), "unchanged_count": 0,
                "replacements": replacements, "failed": missing, "ids_changed": True}

    # -- helpers ------------------------------------------------------------------

    def _ordered(self) -> list[FakeObject]:
        """Objects in creation order, like the editor's object array."""
        return [self.objects[uid] for uid in sorted(self.objects)]
