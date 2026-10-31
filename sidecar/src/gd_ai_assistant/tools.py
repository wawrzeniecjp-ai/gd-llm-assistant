"""The tools exposed to the model, and how each maps to a protocol request.

SPEC.md 6.2.3 fixes the tool set. Two rules shape this module:

* **Tool arguments are the protocol payload.** For every pass-through tool the
  model's arguments *are* the request payload, with no translation layer. That is
  deliberate: a translation step is a second place for a field name to be wrong,
  and the payload schemas are already the validated interface between the two
  processes. `search_object_catalog` is the one exception, because it is answered
  locally and never leaves the sidecar.
* **Tool schemas must match the mod's validator, not the spec's prose.** The
  property list below (`x`, `y`, `rotation`, `scale_x`, `scale_y`, `flip_x`,
  `flip_y`, `z_layer`, `z_order`, `editor_layer`, `groups`) is what
  `isSupportedProperty` in the mod accepts. Note that SPEC.md 7.5's table also
  lists `color_channel_1`/`color_channel_2`, which the implementation does **not**
  accept -- offering them here would produce a guaranteed `UNSUPPORTED_PROPERTY`
  error for any model that believed the prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .providers.base import ToolSpec

#: Object properties the mod accepts for read and write (SPEC.md 7.5, verified in
#: the mod's `kSpecs`). Kept in one place so the tool schema and the prompt agree.
SETTABLE_PROPERTIES: tuple[str, ...] = (
    "x",
    "y",
    "rotation",
    "scale_x",
    "scale_y",
    "flip_x",
    "flip_y",
    "z_layer",
    "z_order",
    "editor_layer",
    "groups",
)

#: Properties the mod accepts in `delta` (relative change). Verified in the mod.
DELTA_PROPERTIES: tuple[str, ...] = ("x", "y", "rotation")

#: `z_layer` names the mod accepts, or an integer cast directly to `ZLayer`.
#: The enum's values are non-contiguous, so a name is strongly preferred.
Z_LAYER_NAMES: tuple[str, ...] = (
    "b5",
    "b4",
    "b3",
    "b2",
    "b1",
    "portal_back",
    "default",
    "t1",
    "t2",
    "t3",
    "t4",
)

_RANGE = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 2,
    "maxItems": 2,
    "description": "Inclusive [min, max] pair.",
}


@dataclass(frozen=True)
class Tool:
    """One tool, its schema, and what the loop must do with it."""

    spec: ToolSpec
    protocol_type: str | None
    """The request type sent to the mod, or None for a sidecar-local tool."""

    mutates_objects: bool
    """Create/modify/delete. Drives the lazy transaction and the per-turn cap.

    `set_selection` is deliberately excluded: selection is not level content, so
    grouping it in the turn's undo entry would put a non-undoable operation on a
    stack of undoable ones.
    """

    is_action: bool
    """Whether this appears in `turn_finished.actions` (SPEC.md 7.4). Includes
    `set_selection`, which the schema's `kind` enum lists."""

    def object_count(self, arguments: dict[str, Any]) -> int:
        """Objects this call would affect, counted from the arguments.

        Counted from the request rather than the response so the per-turn cap can
        refuse *before* mutating: checking afterwards would let the cap be
        exceeded on the very turn it was meant to stop.
        """
        if not self.mutates_objects and not self.is_action:
            return 0
        if self.spec.name == "create_objects":
            objects = arguments.get("objects")
            return len(objects) if isinstance(objects, list) else 0
        targets = arguments.get("targets")
        return len(targets) if isinstance(targets, list) else 0


def _object_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "description": "An object to create.",
        "required": ["id", "x", "y"],
        "properties": {
            "id": {
                "type": "integer",
                "description": "Object type id from search_object_catalog. Do not invent it.",
            },
            "x": {"type": "number", "description": "GD units. 30 units = one grid block."},
            "y": {"type": "number", "description": "GD units, measured up from 0."},
            "properties": _properties_schema("Optional properties set at creation."),
        },
    }


def _properties_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "additionalProperties": False,
        "properties": {
            "x": {"type": "number"},
            "y": {"type": "number"},
            "rotation": {"type": "number", "description": "Degrees."},
            "scale_x": {"type": "number"},
            "scale_y": {"type": "number"},
            "flip_x": {"type": "boolean"},
            "flip_y": {"type": "boolean"},
            "z_layer": {
                "description": "Named z-layer, or an integer cast to the game's ZLayer.",
                "oneOf": [{"type": "string", "enum": list(Z_LAYER_NAMES)}, {"type": "integer"}],
            },
            "z_order": {"type": "integer"},
            "editor_layer": {"type": "integer"},
            "groups": {"type": "array", "items": {"type": "integer"}},
        },
    }


def _build() -> dict[str, Tool]:
    tools = [
        Tool(
            spec=ToolSpec(
                name="search_object_catalog",
                description=(
                    "Look up object type ids by name. The built-in catalog is a small "
                    "seed and will often not have what you need. If it returns nothing, "
                    "use list_object_types to find a type already in the level, or ask "
                    "the user. Never guess an id."
                ),
                parameters={
                    "type": "object",
                    "required": ["query"],
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Name or keyword, e.g. 'spike', 'platform', 'orb'.",
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="list_object_types",
                description=(
                    "List the object TYPES already used in this level: id, count, "
                    "approximate size, and one example position. This is the reliable "
                    "way to learn an object id -- reuse a type the user already placed "
                    "rather than guessing from memory. Check this before create_objects."
                ),
                parameters={"type": "object", "additionalProperties": False, "properties": {}},
            ),
            protocol_type="list_object_types",
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="get_editor_state",
                description=(
                    "Camera position, current editor layer, ground height, grid size "
                    "and selection summary. Call this first when placement matters."
                ),
                parameters={"type": "object", "additionalProperties": False, "properties": {}},
            ),
            protocol_type="get_editor_state",
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="summarize_region",
                description=(
                    "Count objects by type within a rectangle. Prefer this over "
                    "query_objects when you only need to know what is there."
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "x_range": {**_RANGE, "description": "[min, max] in GD units."},
                        "y_range": {**_RANGE, "description": "[min, max] in GD units."},
                        "editor_layer": {"type": "integer"},
                    },
                },
            ),
            protocol_type="summarize_region",
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="query_objects",
                description=(
                    "List objects matching filters, paged. Returns compact records "
                    "with only non-default properties."
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "filters": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "id": {"type": "integer", "description": "Object type id."},
                                "ids": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": "Specific object ids (unique ids).",
                                },
                                "editor_layer": {"type": "integer"},
                                "z_layer": {"type": "integer"},
                                "group": {"type": "integer"},
                                "x_range": _RANGE,
                                "y_range": _RANGE,
                                "selected_only": {"type": "boolean"},
                            },
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 100,
                        },
                        "cursor": {"type": "string"},
                    },
                },
            ),
            protocol_type="query_objects",
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="get_object_properties",
                description="Full property set for one object, by its unique id.",
                parameters={
                    "type": "object",
                    "required": ["object_id"],
                    "additionalProperties": False,
                    "properties": {
                        "object_id": {
                            "type": "integer",
                            "description": "The object's unique id, not its type id.",
                        }
                    },
                },
            ),
            protocol_type="get_object_properties",
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="create_objects",
                description=(
                    "Create objects in one batch. Validated whole before anything is "
                    "changed, so a bad entry creates nothing."
                ),
                parameters={
                    "type": "object",
                    "required": ["objects"],
                    "additionalProperties": False,
                    "properties": {"objects": {"type": "array", "items": _object_schema()}},
                },
            ),
            protocol_type="create_objects",
            mutates_objects=True,
            is_action=True,
        ),
        Tool(
            spec=ToolSpec(
                name="modify_objects",
                description=(
                    "Change properties on existing objects. Use `delta` for relative "
                    "moves; use `set` for absolute values. Both may be given."
                ),
                parameters={
                    "type": "object",
                    "required": ["targets"],
                    "additionalProperties": False,
                    "properties": {
                        "targets": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Object unique ids.",
                        },
                        "set": _properties_schema("Absolute property values."),
                        "delta": {
                            "type": "object",
                            "description": "Relative changes. Only x, y and rotation.",
                            "additionalProperties": False,
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "rotation": {"type": "number"},
                            },
                        },
                    },
                },
            ),
            protocol_type="modify_objects",
            mutates_objects=True,
            is_action=True,
        ),
        Tool(
            spec=ToolSpec(
                name="delete_objects",
                description="Delete objects by unique id. Missing ids are reported, not fatal.",
                parameters={
                    "type": "object",
                    "required": ["targets"],
                    "additionalProperties": False,
                    "properties": {"targets": {"type": "array", "items": {"type": "integer"}}},
                },
            ),
            protocol_type="delete_objects",
            mutates_objects=True,
            is_action=True,
        ),
        Tool(
            spec=ToolSpec(
                name="set_selection",
                description=(
                    "Select objects so the user can see what you are referring to. "
                    "Useful before an edit and when answering a question about "
                    "specific objects."
                ),
                parameters={
                    "type": "object",
                    "required": ["targets"],
                    "additionalProperties": False,
                    "properties": {"targets": {"type": "array", "items": {"type": "integer"}}},
                },
            ),
            protocol_type="set_selection",
            mutates_objects=False,
            is_action=True,
        ),
    ]
    return {tool.spec.name: tool for tool in tools}


TOOLS: dict[str, Tool] = _build()


def tool_specs() -> list[ToolSpec]:
    """The tool list handed to the provider, in a stable order."""
    return [tool.spec for tool in TOOLS.values()]


def get(name: str) -> Tool | None:
    return TOOLS.get(name)


def unknown_tool_names(names: list[str]) -> list[str]:
    """Names the model asked for that do not exist.

    Reported back as a failed tool result rather than raised: a hallucinated tool
    name is something the model can recover from if it is told, and killing the
    turn over it wastes the work already done.
    """
    return [name for name in names if name not in TOOLS]
