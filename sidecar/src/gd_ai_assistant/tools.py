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

from collections.abc import Collection
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
    "color_channel_1",
    "color_channel_2",
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

    requires: tuple[str, ...] = ()
    """Mod request types this tool needs besides `protocol_type`.

    A local tool that reads the level snapshot needs `get_level_objects`, for instance.
    When the connected mod answers any of them with `UNSUPPORTED_REQUEST`, the tool is
    withdrawn for the session (LEVEL-DESIGN-SPEC.md LD14) rather than offered and failed.
    """

    def needed_requests(self) -> tuple[str, ...]:
        own = (self.protocol_type,) if self.protocol_type is not None else ()
        return own + self.requires

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
            "color_channel_1": {
                "type": "integer",
                "minimum": 0,
                "maximum": 999,
                "description": (
                    "Colour-channel id from the level's colour page, not a colour. "
                    "The object points at the channel; change the channel to recolour "
                    "everything using it."
                ),
            },
            "color_channel_2": {
                "type": "integer",
                "minimum": 0,
                "maximum": 999,
                "description": (
                    "Detail colour-channel id. Only object types with a detail colour "
                    "accept this; on the rest the write is refused."
                ),
            },
        },
    }


def _build() -> dict[str, Tool]:
    tools = [
        Tool(
            spec=ToolSpec(
                name="search_object_catalog",
                description=(
                    "Look up object type ids by name or description. Backed by the "
                    "game's own object table, so the ids are real. Each result gives "
                    "`id`, `name`, `category` (blocks, hazards, orbs, ...), `frame` "
                    "(the sprite the game draws) and `grid_size` in GD units. "
                    "Use `grid_size` and `frame` to choose between similar objects: "
                    "a 'thin' or 'small' spike is a spike with a smaller size, and "
                    "the response tells you which query words matched nothing. "
                    "Never invent an id; if nothing matches, say so."
                ),
                parameters={
                    "type": "object",
                    "required": ["query"],
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Words describing the object, e.g. 'spike', 'slope', "
                                "'orb', 'colour trigger'. Words that name nothing are "
                                "reported back so you can narrow by size instead."
                            ),
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
                name="search_properties",
                description=(
                    "Look up what a save-string property means, or find the property that "
                    "controls something. Two ways in: pass `query` with a name fragment "
                    "('duration', 'ease', 'target') to find properties, or pass `key` "
                    "with a save-string key number to find out what it means. "
                    "Properties are C++ field names, so search the meaningful part "
                    "(`duration`, not `m_duration`). This is how you learn trigger "
                    "settings you have no named property for."
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Name fragment, e.g. 'duration', 'easing', 'target'.",
                        },
                        "key": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Save-string key to look up, e.g. 51.",
                        },
                        "class": {
                            "type": "string",
                            "description": (
                                "Optional class to search within, e.g. 'EffectGameObject'. "
                                "Omit to search every class -- the mod does not report "
                                "object classes, so you usually will not have one."
                            ),
                        },
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="describe_class",
                description=(
                    "List every save-string property a game object class has, inherited "
                    "ones included. Use it when you need the full set of settings for a "
                    "kind of object rather than one by name. Classes are named like "
                    "`GameObject`, `EffectGameObject`, `RingObject`."
                ),
                parameters={
                    "type": "object",
                    "required": ["class"],
                    "additionalProperties": False,
                    "properties": {"class": {"type": "string", "description": "Class name."}},
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="capture_screenshot",
                description=(
                    "Look at the level. Renders the editor view and shows it to you as an "
                    "image. Two choices of region: `screen` for the whole view -- good for "
                    "overall composition, whether things line up, whether a section reads "
                    "clearly -- or `selection`, a box around the selected objects, padded by "
                    "`radius` points. `selection` is the one to use for detail: it is a "
                    "smaller crop, so it arrives at higher effective resolution. The chat "
                    "panel is hidden for the capture, so you see the level, not yourself. "
                    "The editor's own toolbars and buttons are hidden too, so what you get "
                    "is the level and nothing else; pass `include_ui: true` only if the "
                    "question is specifically about the editor's interface. "
                    "The result says which part of the level the image covers "
                    "(`viewport`, in level coordinates), so you can connect what you see "
                    "to object positions without guessing. "
                    "Use it when a question is visual: 'does this look right', 'is anything "
                    "floating', 'is there a gap'."
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "region": {
                            "type": "string",
                            "enum": ["screen", "selection"],
                            "default": "screen",
                            "description": (
                                "'screen' for the whole view, 'selection' around the "
                                "selected objects."
                            ),
                        },
                        "radius": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 2000,
                            "default": 120,
                            "description": (
                                "Padding around the selection, in points. Ignored for 'screen'."
                            ),
                        },
                        "include_ui": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Also capture the editor's toolbars and buttons. Off by "
                                "default, because they take up a large part of the frame "
                                "and invite you to describe them instead of the level."
                            ),
                        },
                    },
                },
            ),
            protocol_type="capture_screenshot",
            mutates_objects=False,
            is_action=True,
        ),
        Tool(
            spec=ToolSpec(
                name="get_camera",
                description=(
                    "Where the editor is currently looking: `x` and `y` are level "
                    "coordinates of the centre of the view, `zoom` is the magnification, "
                    "and `visible` is the rectangle of the level actually on screen "
                    "(`x1`, `y1`, `x2`, `y2`, in level coordinates -- the same space "
                    "objects use). Use `visible` to decide whether something is worth "
                    "looking at before you capture, or to work out that an object is just "
                    "off the edge."
                ),
                parameters={"type": "object", "additionalProperties": False, "properties": {}},
            ),
            protocol_type="get_camera",
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="set_camera",
                description=(
                    "Move or zoom the editor view. `x`/`y` are the level coordinates to "
                    "centre on -- the same coordinates objects have, so the ground is "
                    "around y=0 and most building happens between y=-30 and y=300. `zoom` "
                    "magnifies (1 is normal, 2 is twice as close); it is clamped to "
                    "0.05..4 rather than refused. Pass only the parts you are changing. "
                    "This moves the user's screen: say where you are moving it before or "
                    "while you do, and put it back when you are done looking. Pair it with "
                    "`capture_screenshot` -- pan somewhere, then look at it."
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "minProperties": 1,
                    "properties": {
                        "x": {
                            "type": "number",
                            "description": "Level x to centre on.",
                        },
                        "y": {
                            "type": "number",
                            "description": "Level y to centre on.",
                        },
                        "zoom": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "description": "Magnification; 1 is the default view.",
                        },
                    },
                },
            ),
            protocol_type="set_camera",
            mutates_objects=False,
            is_action=True,
        ),
        Tool(
            spec=ToolSpec(
                name="get_level_settings",
                description=(
                    "Read the level's own settings: background/ground/middleground "
                    "patterns, starting gamemode, the song, and the flags that change how "
                    "the level plays. Call this before changing any of them, so you know "
                    "what you are overwriting."
                ),
                parameters={"type": "object", "additionalProperties": False, "properties": {}},
            ),
            protocol_type="get_level_settings",
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="set_level_settings",
                description=(
                    "Change the level's own settings. Pass only the fields you are "
                    "changing; the rest keep their values. Covers bg/ground patterns, "
                    "`start_mode` (0=cube, 1=ship, 2=ball, 3=ufo, 4=wave, 5=robot, "
                    "6=spider, 7=swing, 8=jetpack), `audio_track` (index into the "
                    "normal song catalogue), `song_offset`, and `platformer`, "
                    "`two_player`, `mirror`, `reverse`, `start_mini`, `start_dual`. "
                    "The song catalogue itself cannot be changed."
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "minProperties": 1,
                    "properties": {
                        "background": {"type": "integer", "minimum": 0, "maximum": 100},
                        "ground": {"type": "integer", "minimum": 0, "maximum": 100},
                        "ground_line": {"type": "integer", "minimum": 0, "maximum": 50},
                        "middle_ground": {"type": "integer", "minimum": 0, "maximum": 100},
                        "start_mode": {"type": "integer", "minimum": 0, "maximum": 12},
                        "start_mini": {"type": "boolean"},
                        "start_dual": {"type": "boolean"},
                        "two_player": {"type": "boolean"},
                        "platformer": {"type": "boolean"},
                        "reverse": {"type": "boolean"},
                        "rotate_gameplay": {"type": "boolean"},
                        "mirror": {"type": "boolean"},
                        "song_offset": {
                            "type": "number",
                            "description": "Seconds between the level start and the song.",
                        },
                        "audio_track": {
                            "type": "integer",
                            "minimum": 0,
                            "description": (
                                "Index into the normal song catalogue. Refused if the "
                                "level uses a custom catalogue."
                            ),
                        },
                    },
                },
            ),
            protocol_type="set_level_settings",
            mutates_objects=False,
            is_action=True,
        ),
        Tool(
            spec=ToolSpec(
                name="get_save_properties",
                description=(
                    "Read **every** property of one object, named where the game's "
                    "bindings name it. Use this for settings you have no named property "
                    "for -- a trigger's flags, durations, easing -- and to see a "
                    "property's current value before changing it. Companion to "
                    "set_save_property."
                ),
                parameters={
                    "type": "object",
                    "required": ["object_id"],
                    "additionalProperties": False,
                    "properties": {
                        "object_id": {
                            "type": "integer",
                            "description": "The object's unique id.",
                        }
                    },
                },
            ),
            protocol_type="get_save_properties",
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="set_save_property",
                description=(
                    "Write any property of an object by its save-string key, including "
                    "settings this mod has no named property for. Pass `key` (a number) "
                    "or `property` (a name such as 'no touch', 'duration', 'easing') -- "
                    "a name is resolved against the game's own field names. "
                    "**This rebuilds the object, so its unique id changes**; the result "
                    "gives the new id and you must use it from then on. Not for "
                    "position or type: use modify_objects for x/y/rotation."
                ),
                parameters={
                    "type": "object",
                    "required": ["targets"],
                    "additionalProperties": False,
                    "properties": {
                        "targets": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 1,
                            "description": "Object unique ids.",
                        },
                        "key": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Save-string key, e.g. 121. Prefer `property` unless you "
                                "already have the key."
                            ),
                        },
                        "property": {
                            "type": "string",
                            "description": (
                                "Property name, e.g. 'no touch', 'duration', 'easing'. "
                                "Used when `key` is absent."
                            ),
                        },
                        "value": {
                            "type": ["string", "number", "boolean"],
                            "description": (
                                "New value. Booleans become 1/0. For encoded properties "
                                "(group lists, HSV, text) pass the exact text the save "
                                "string should carry."
                            ),
                        },
                    },
                },
            ),
            protocol_type="set_save_property",
            mutates_objects=True,
            is_action=True,
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
                name="get_color_channel",
                description=(
                    "Read a colour channel from the level's colour page. Every object "
                    "whose `color_channel_1` (base) or `color_channel_2` (detail) is "
                    "this id shows this one colour, so this tells you what colour a "
                    "group of objects is, and which channel to name when the user asks "
                    "for a colour change. `exists: false` means the level has never "
                    "set the channel: it shows white (255, 255, 255)."
                ),
                parameters={
                    "type": "object",
                    "required": ["channel"],
                    "additionalProperties": False,
                    "properties": {
                        "channel": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Colour channel id, e.g. 3.",
                        }
                    },
                },
            ),
            protocol_type="get_color_channel",
            mutates_objects=False,
            is_action=False,
        ),
        Tool(
            spec=ToolSpec(
                name="set_color_channel",
                description=(
                    "Set a colour channel's colour, which is how you recolour things: "
                    "objects borrow a channel's colour, so changing the channel "
                    "recolours **every** object using it -- say so when the user meant "
                    "one object. Channels 1-999 are custom; 1000+ are the system "
                    "channels (BG, ground, ground line, middleground). Read with "
                    "get_color_channel first if you need to know what you are "
                    "overwriting. RGB is 0-255."
                ),
                parameters={
                    "type": "object",
                    "required": ["channel", "red", "green", "blue"],
                    "additionalProperties": False,
                    "properties": {
                        "channel": {"type": "integer", "minimum": 1},
                        "red": {"type": "integer", "minimum": 0, "maximum": 255},
                        "green": {"type": "integer", "minimum": 0, "maximum": 255},
                        "blue": {"type": "integer", "minimum": 0, "maximum": 255},
                    },
                },
            ),
            protocol_type="set_color_channel",
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
        *_design_tools(),
    ]
    return {tool.spec.name: tool for tool in tools}


#: Local tools built on the level snapshot (LEVEL-DESIGN-SPEC.md 5, 13). The agent runs
#: them itself; `place_grid` then sends ordinary `delete_objects`/`create_objects`.
DESIGN_TOOLS = frozenset(
    {"render_grid", "place_grid", "render_schematic", "verify_section", "simulate_inputs",
     "playtest_verify", "get_level_plan", "update_level_plan", "beat_grid"}
)


def _design_tools() -> list[Tool]:
    layers = {
        "type": "array",
        "items": {"type": "string", "enum": ["gameplay", "triggers", "deco"]},
        "description": "Which objects to include. Default: gameplay only.",
    }
    cell_range = {
        "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
        "description": "Inclusive [first, last] pair, in grid cells.",
    }
    return [
        Tool(
            spec=ToolSpec(
                name="render_grid",
                description=(
                    "Read a stretch of the level as GridText: one character per 30-unit "
                    "cell, top row first, under a header `@grid col=C row=R w=W h=H` giving "
                    "the bottom-left cell. Row 0 is the row standing on the ground. Use it "
                    "to understand gameplay structure before changing it, and as the "
                    "starting point for `place_grid`. Objects GridText cannot show exactly "
                    "(off-centre, rotated by a non-right angle, scaled) appear as `?` and "
                    "are listed in `extras`; nothing is silently snapped. Two gameplay "
                    "objects in one cell are listed in `overlaps`. Objects without a "
                    "built-in character get a custom one, and `legend_for_place_grid` "
                    "holds the legend that recreates them. Limits: 200 columns, 40 rows."
                ),
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "x_range": {**_RANGE, "description": "Level units, [min, max]."},
                        "y_range": {
                            **_RANGE,
                            "description": "Level units. Default: from the ground up to "
                            "two rows above the highest gameplay object.",
                        },
                        "cols": cell_range,
                        "rows": cell_range,
                        "layers": layers,
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
            requires=("get_level_objects",),
        ),
        Tool(
            spec=ToolSpec(
                name="place_grid",
                description=(
                    "Build gameplay from a GridText block (same format `render_grid` "
                    "returns). Each character places one object at its cell centre. "
                    "`mode: add` (default) only fills non-empty cells and refuses if any "
                    "of them already holds a gameplay object, listing the clashes. "
                    "`mode: replace` first deletes every gameplay object in the block's "
                    "rectangle (deco and triggers are kept), then builds. `.` is empty, "
                    "`:` marks a cell covered by a tall object anchored elsewhere (it is "
                    "ignored), `?` cannot be written. `preview: true` changes nothing and "
                    "returns the objects it would create. Everything placed is one undo "
                    "step with the rest of the turn."
                ),
                parameters={
                    "type": "object",
                    "required": ["grid"],
                    "additionalProperties": False,
                    "properties": {
                        "grid": {"type": "string", "description": "One GridText block."},
                        "legend": {
                            "type": "object",
                            "description": (
                                "Custom characters, e.g. {\"a\": {\"id\": 1338, "
                                "\"rotation\": 90}}. Keys: id (required), rotation "
                                "(multiple of 90), flip_x, flip_y, properties (z_layer, "
                                "z_order, editor_layer, groups, color_channel_1/2). "
                                "Built-in characters cannot be redefined."
                            ),
                        },
                        "mode": {"type": "string", "enum": ["add", "replace"],
                                 "default": "add"},
                        "preview": {"type": "boolean", "default": False},
                        "snap_to_beats": {
                            "type": "object",
                            "description": (
                                "Report how far each orb, and each press of the verified "
                                "route, is from the nearest beat (needs the plan's bpm). "
                                "A report only: nothing is moved."
                            ),
                            "properties": {"subdivision": {"type": "integer", "minimum": 1,
                                                           "maximum": 8, "default": 2}},
                            "additionalProperties": False,
                        },
                        "verify": {
                            "type": "object",
                            "description": (
                                "Simulate the block (plus 10 columns either side) and "
                                "attach the result: is it playable, the presses, how much "
                                "timing slack each has. Also works with preview. Placement "
                                "is not blocked by a failure: fix it or tell the user."
                            ),
                            "properties": {
                                "gamemode": {"type": "string", "default": "cube"},
                                "speed": {"type": "string",
                                          "enum": ["0.5x", "1x", "2x", "3x", "4x"],
                                          "default": "1x"},
                                "entry_row": {"type": "integer", "minimum": 0, "default": 0},
                            },
                            "additionalProperties": False,
                        },
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=True,
            is_action=True,
            requires=("get_level_objects", "create_objects", "delete_objects"),
        ),
        Tool(
            spec=ToolSpec(
                name="verify_section",
                description=(
                    "Check that a stretch of the level can actually be played, with the "
                    "game's measured physics. Finds a route (the presses), and for each "
                    "press its timing window: how many ms early or late it can be. "
                    "Results: possible (with route, windows, difficulty and the exit "
                    "state), impossible (how far the furthest attempt got and what killed "
                    "it), inconclusive (search cut short; not a no), unverifiable (the "
                    "stretch contains something not simulated yet; `reason` says what. The "
                    "cube with solids, hazards, yellow/pink/blue orbs and pads and speed, "
                    "gravity, size and mode portals is simulated; so are ship, ball, ufo "
                    "and wave, mini or not, with blocks, spikes, orbs and pads; and the "
                    "robot, upright or upside down; and the spider (a press teleports it "
                    "to the surface opposite, or onto a spike in its way). Not yet: a "
                    "mini cube's or robot's orbs, a mini cube squeezed under a low "
                    "block, and the swing). A ship, wave or robot "
                    "route has holds (the robot jumps "
                    "higher the longer it holds): `action` hold, `release_tick`, "
                    "`release_window_ms`. "
                    "`deaths_without_input` says where a run with no presses dies: the "
                    "first obstacle. A window of one tick (~4 ms) is frame-perfect: too "
                    "hard for any normal level."
                ),
                parameters={
                    "type": "object",
                    "required": ["x_range"],
                    "additionalProperties": False,
                    "properties": {
                        "x_range": {**_RANGE, "description": "Level units, [start, end]."},
                        "difficulty": {
                            "type": ["integer", "string"],
                            "description": (
                                "The difficulty to judge against: 1-10 on GD's star scale (2 "
                                "easy, 3 normal, 4-5 hard, 6-7 harder, 8-9 insane, 10 demon), "
                                "or one of those words. Use the plan section's difficulty. "
                                "Adds `targets`, `violations` and `in_band`."
                            ),
                        },
                        "start_from": {
                            "type": "string", "enum": ["level_start", "explicit"],
                            "default": "level_start",
                            "description": (
                                "level_start plays from the beginning of the level and "
                                "lists the presses inside x_range; explicit starts at "
                                "x_range[0] in the given state."
                            ),
                        },
                        "start": {
                            "type": "object",
                            "properties": {
                                "mode": {"type": "string", "default": "cube"},
                                "speed": {"type": "string",
                                          "enum": ["0.5x", "1x", "2x", "3x", "4x"]},
                                "row": {"type": "integer", "minimum": 0},
                                "vy": {"type": "number"},
                                "gravity": {"type": "integer", "enum": [1, -1]},
                                "mini": {"type": "boolean"},
                            },
                            "additionalProperties": False,
                        },
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
            requires=("get_level_objects",),
        ),
        Tool(
            spec=ToolSpec(
                name="simulate_inputs",
                description=(
                    "Play your own presses in the simulator, with the game's measured "
                    "physics, and see what happens: where the cube goes, where it lands, "
                    "and where and on what it dies. Use it to test a specific timing (for "
                    "example a press a few units earlier than verify_section's route) or "
                    "to explain why a section fails. Inputs are a press or release at a "
                    "`tick` (240 per second, counted from the start) or at an `x` (the "
                    "first tick the cube reaches that x). A press jumps if the cube is on "
                    "the ground or touching an orb; holding keeps jumping on landing, so "
                    "follow each press with a release unless you mean to hold. Returns "
                    "the outcome (reached, death, max_ticks), events (presses, takeoffs, "
                    "landings, orb/pad impulses, speed changes) and a sampled trace."
                ),
                parameters={
                    "type": "object",
                    "required": ["until_x"],
                    "additionalProperties": False,
                    "properties": {
                        "until_x": {"type": "number",
                                    "description": "Stop when the cube reaches this x."},
                        "inputs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["action"],
                                "additionalProperties": False,
                                "properties": {
                                    "action": {"type": "string",
                                               "enum": ["press", "release"]},
                                    "tick": {"type": "integer", "minimum": 0},
                                    "x": {"type": "number"},
                                },
                            },
                        },
                        "start_from": {
                            "type": "string", "enum": ["level_start", "explicit"],
                            "default": "level_start",
                            "description": (
                                "level_start plays from the beginning of the level; "
                                "explicit starts at start.x in the given state, at tick 0."
                            ),
                        },
                        "start": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "number"},
                                "mode": {"type": "string", "default": "cube"},
                                "speed": {"type": "string",
                                          "enum": ["0.5x", "1x", "2x", "3x", "4x"]},
                                "row": {"type": "integer", "minimum": 0},
                                "vy": {"type": "number"},
                                "gravity": {"type": "integer", "enum": [1, -1]},
                                "mini": {"type": "boolean"},
                            },
                            "additionalProperties": False,
                        },
                        "sample_every": {"type": "integer", "minimum": 1, "default": 4,
                                         "description": "Trace stride in ticks."},
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
            requires=("get_level_objects",),
        ),
        Tool(
            spec=ToolSpec(
                name="playtest_verify",
                description=(
                    "Play a section in the real game: the route verify_section found is "
                    "replayed in the editor's playtest, press for press, and the result "
                    "says whether the game agrees. `confirmed`: the game survived the "
                    "route, the strongest check there is. `failed_in_game`: the simulator "
                    "was wrong here; say so, do not call the section verified. "
                    "`not_played_*`: the solver found no route, so nothing was played. "
                    "Use it ONLY when the user asked you to playtest or test in the game in "
                    "this message: the user watches the editor play by itself, and it "
                    "takes the section's length in real time. It cannot run in a turn that "
                    "has already changed the level (finish the turn first). The replay "
                    "starts from the nearest start position at or before x_range[0], or the "
                    "level start; the user can press any key to take over, which cancels it."
                ),
                parameters={
                    "type": "object",
                    "required": ["x_range"],
                    "additionalProperties": False,
                    "properties": {
                        "x_range": {**_RANGE, "description": "Level units, [start, end]."},
                        "start": {
                            "anyOf": [
                                {"type": "string", "enum": ["auto", "level_start"]},
                                {"type": "object", "required": ["start_pos"],
                                 "additionalProperties": False,
                                 "properties": {"start_pos": {"type": "integer"}}},
                            ],
                            "description": (
                                "'auto' (default): the nearest start position at or before "
                                "x_range[0], else the level start. Or 'level_start', or "
                                "{\"start_pos\": uid}."
                            ),
                        },
                        "edges": {
                            "type": "boolean", "default": False,
                            "description": (
                                "Also replay each press at both ends of its timing window "
                                "and one tick past each: checks the windows too. Four extra "
                                "runs per press."
                            ),
                        },
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
            requires=("get_level_objects", "run_replay"),
        ),
        Tool(
            spec=ToolSpec(
                name="render_schematic",
                description=(
                    "Draw a stretch of the level as a diagram and look at it: hitboxes, "
                    "not sprites, so what you see is what collides. Solids blue, hazards "
                    "red, orbs and pads as coloured circles, portals as labelled outlined "
                    "bars, deco faint grey, triggers as ticks along the top, the ground in "
                    "brown, with a cell grid and column numbers. Works without the game's "
                    "visuals getting in the way: use it to judge structure, spacing and "
                    "flow over a longer stretch than GridText shows comfortably. Up to "
                    "1600x600 px; wide ranges are drawn smaller."
                ),
                parameters={
                    "type": "object",
                    "required": ["x_range"],
                    "additionalProperties": False,
                    "properties": {
                        "x_range": {**_RANGE, "description": "Level units, [min, max]."},
                        "y_range": {**_RANGE, "description": "Level units. Default: auto."},
                        "layers": {**layers, "description": "Default: gameplay and deco."},
                        "overlays": {
                            "type": "array",
                            "items": {"type": "string", "enum": [
                                "grid", "route", "windows", "beats", "sections", "camera"]},
                            "description": "Default: grid. `route` draws the simulated "
                            "path from the level start with presses as dots; `windows` "
                            "shows each press's timing window as a green bar; `beats` "
                            "draws the plan's bars (thick), beats (thin) and half-beats "
                            "(ticks) in purple; `sections` labels the plan's sections "
                            "along the top. camera arrives later.",
                        },
                        "px_per_cell": {"type": "integer", "minimum": 4, "maximum": 40,
                                        "default": 12},
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
            requires=("get_level_objects",),
        ),
        *_plan_tools(),
    ]


def _plan_tools() -> list[Tool]:
    """The level plan, the beat grid and the song (LEVEL-DESIGN-SPEC.md 8, 9)."""
    section_fields = (
        "id, name, start_ms/end_ms (song time; x_range is derived from them), mode, "
        "speed, gravity, mini, difficulty (1-10), intensity (0-1), style, status "
        "{gameplay, deco, effects: todo|in_progress|built|verified|needs_fix}, notes"
    )
    return [
        Tool(
            spec=ToolSpec(
                name="get_level_plan",
                description=(
                    "Read the level plan: the durable design record for this level "
                    "(concept, target difficulty, music, sections with their status, "
                    "decisions). Each section's x_range is derived from its song times "
                    "through the speed portals, sections whose gameplay changed since "
                    "they were verified are marked stale, and `progress` sums it up. "
                    "Empty until something is written with update_level_plan."
                ),
                parameters={"type": "object", "additionalProperties": False,
                            "properties": {}},
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
            requires=("get_level_objects",),
        ),
        Tool(
            spec=ToolSpec(
                name="update_level_plan",
                description=(
                    "Change the level plan. It is design metadata kept by the assistant, "
                    "not part of the level and not undone by Revert. Ops apply in order "
                    "and one invalid op rejects the whole batch: set_field {path, value} "
                    "(concept, target_difficulty, allow_replay, todo, ...), add_section "
                    "{section, after?}, update_section {id, fields}, remove_section {id}, "
                    "add_decision {text}, set_music {bpm, offset_ms, beats_per_bar, "
                    "source, tempo_changes}, set_style {name, profile}. Section fields: "
                    f"{section_fields}. Setting status.gameplay to verified records what "
                    "the section looks like now, so a later change marks it stale. "
                    "Record the user's decisions as you go; never guess a bpm."
                ),
                parameters={
                    "type": "object",
                    "required": ["ops"],
                    "additionalProperties": False,
                    "properties": {
                        "ops": {
                            "type": "array", "minItems": 1, "maxItems": 50,
                            "items": {
                                "type": "object",
                                "required": ["op"],
                                "properties": {"op": {"type": "string", "enum": [
                                    "set_field", "add_section", "update_section",
                                    "remove_section", "add_decision", "set_music",
                                    "set_style"]}},
                            },
                        },
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
            requires=("get_level_objects",),
        ),
        Tool(
            spec=ToolSpec(
                name="beat_grid",
                description=(
                    "Where the beats fall in the level: each beat (and subdivision) in "
                    "x_range with its song time, x, column, bar and strength (bar, beat, "
                    "sub), from the plan's bpm and the level's speed portals and song "
                    "offset. Also how many columns one beat spans at the start speed. "
                    "Use it to put jumps and orbs on the beat. Needs the bpm in the plan."
                ),
                parameters={
                    "type": "object",
                    "required": ["x_range"],
                    "additionalProperties": False,
                    "properties": {
                        "x_range": {**_RANGE, "description": "Level units, [start, end]."},
                        "subdivision": {"type": "integer", "minimum": 1, "maximum": 8,
                                        "default": 1,
                                        "description": "2 adds the half beats, 4 quarters."},
                    },
                },
            ),
            protocol_type=None,
            mutates_objects=False,
            is_action=False,
            requires=("get_level_objects",),
        ),
        Tool(
            spec=ToolSpec(
                name="get_level_music",
                description=(
                    "The level's song: official track or custom song id, the song "
                    "offset, its file and length when the game knows them, and the "
                    "editor's music guidelines if it can read them. It does not tell "
                    "you the bpm: ask the user for that."
                ),
                parameters={"type": "object", "additionalProperties": False,
                            "properties": {}},
            ),
            protocol_type="get_level_music",
            mutates_objects=False,
            is_action=False,
        ),
    ]


TOOLS: dict[str, Tool] = _build()


def tool_specs(unsupported: Collection[str] = ()) -> list[ToolSpec]:
    """The tool list handed to the provider, in a stable order.

    Tools that depend on a request the connected mod has said it does not implement
    are left out, so the model is never offered something that can only fail.
    """
    blocked = frozenset(unsupported)
    return [
        tool.spec
        for tool in TOOLS.values()
        if not blocked.intersection(tool.needed_requests())
    ]


def get(name: str) -> Tool | None:
    return TOOLS.get(name)


def unknown_tool_names(names: list[str]) -> list[str]:
    """Names the model asked for that do not exist.

    Reported back as a failed tool result rather than raised: a hallucinated tool
    name is something the model can recover from if it is told, and killing the
    turn over it wastes the work already done.
    """
    return [name for name in names if name not in TOOLS]
