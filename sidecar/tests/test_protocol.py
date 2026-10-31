"""Protocol schema conformance tests (SPEC.md 13).

These assert that the schemas are internally consistent and that the example
frames from SPEC.md 7 actually validate. They are the guard against the mod and
sidecar drifting apart, since the mod itself cannot run headless in CI.
"""

from __future__ import annotations

import json

import pytest

from gd_ai_assistant import protocol


def test_every_schema_loads_and_is_valid_jsonschema() -> None:
    """Each schema file must parse and be a valid draft 2020-12 schema."""
    from jsonschema import Draft202012Validator

    directory = protocol.protocol_dir()
    schemas = sorted(directory.glob("*.schema.json"))
    assert schemas, f"no schemas found in {directory}"

    for path in schemas:
        contents = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(contents)


def test_registry_covers_every_message_in_the_spec() -> None:
    """The registry must list every type from SPEC.md 7.4, 7.5 and 7.6."""
    expected = {
        # handshake
        "hello",
        "response",
        # chat, mod -> sidecar
        "user_message",
        "cancel_turn",
        "transaction_reverted",
        # chat, sidecar -> mod
        "turn_started",
        "assistant_message",
        "assistant_message_delta",
        "turn_finished",
        "sidecar_status",
        "system_message",
        "history",
        # editor requests, sidecar -> mod
        "get_editor_state",
        "query_objects",
        "summarize_region",
        "get_object_properties",
        "create_objects",
        "modify_objects",
        "delete_objects",
        "set_selection",
        "begin_transaction",
        "end_transaction",
        "revert_transaction",
        "ping",
        # editor requests, sidecar -> mod (read-only, no editor required)
        "list_object_catalog",
        # colour channels: level state, not object state
        "get_color_channel",
        "set_color_channel",
        # events, mod -> sidecar
        "editor_closed",
        "level_changed",
        "selection_changed",
        "progress",
        "error",
    }
    assert set(protocol.registry_entries()) == expected


def test_requests_declare_a_result_and_events_do_not() -> None:
    """`result` presence is what distinguishes a request from an event (7.3)."""
    for spec in protocol.registry_entries().values():
        if spec.type == "response":
            continue
        if spec.is_request:
            assert spec.result_ref, f"{spec.type} looks like a request but has no result"
        else:
            assert spec.type in {
                "user_message",
                "cancel_turn",
                "transaction_reverted",
                "turn_started",
                "assistant_message",
                "assistant_message_delta",
                "turn_finished",
                "sidecar_status",
                "system_message",
                "history",
                "editor_closed",
                "level_changed",
                "selection_changed",
                "progress",
                "error",
            }


HELLO_REQUEST = {
    "type": "hello",
    "id": "m-1",
    "payload": {
        "protocol_version": 1,
        "token": "0" * 43,
        "client": {"mod_version": "0.1.0", "gd_version": "2.2081", "geode_version": "5.11.1"},
        "level": {"key": "My Level", "name": "My Level", "object_count": 1284},
    },
}

HELLO_RESPONSE = {
    "type": "response",
    "id": "m-1",
    "payload": {
        "ok": True,
        "result": {
            "protocol_version": 1,
            "sidecar_version": "0.1.0",
            "state": "ready",
            "detail": None,
            "history_count": 12,
        },
    },
}

TURN_FINISHED = {
    "type": "turn_finished",
    "id": None,
    "payload": {
        "turn_id": "t-1",
        "status": "completed",
        "actions": [
            {"kind": "create_objects", "count": 5, "summary": "Created 5 objects (spikes) at y=120"}
        ],
        "txn_id": "x-1",
        "usage": {"input_tokens": 900, "output_tokens": 120, "tool_rounds": 3},
    },
}

CREATE_OBJECTS = {
    "type": "create_objects",
    "id": "s-7",
    "payload": {
        "objects": [
            {"id": 8, "x": 300.0, "y": 105.0},
            {"id": 8, "x": 330.0, "y": 105.0, "properties": {"groups": [3], "z_layer": "b1"}},
        ]
    },
}

LEVEL_CHANGED = {
    "type": "level_changed",
    "id": None,
    "payload": {"added": 5, "removed": 0, "modified": 0, "source": "assistant"},
}


@pytest.mark.parametrize(
    "frame",
    [HELLO_REQUEST, HELLO_RESPONSE, TURN_FINISHED, CREATE_OBJECTS, LEVEL_CHANGED],
)
def test_spec_examples_validate(frame: dict) -> None:
    """The example frames from SPEC.md 7 must validate against the schemas."""
    assert protocol.validate_frame(frame) == []


def test_wrong_token_type_is_rejected() -> None:
    frame = json.loads(json.dumps(HELLO_REQUEST))
    frame["payload"]["token"] = 12345
    assert protocol.validate_payload(frame) != []


def test_unknown_error_code_is_rejected() -> None:
    frame = {
        "type": "response",
        "id": "s-1",
        "payload": {"ok": False, "error": {"code": "NOT_A_REAL_CODE", "message": "x"}},
    }
    assert protocol.validate_payload(frame) != []


def test_unknown_message_type_is_rejected() -> None:
    assert protocol.validate_payload({"type": "not_a_type", "id": None, "payload": {}}) != []


def test_unsupported_property_key_is_rejected() -> None:
    """Unknown property keys must be UNSUPPORTED_PROPERTY, so the schema forbids them (7.5)."""
    frame = json.loads(json.dumps(CREATE_OBJECTS))
    frame["payload"]["objects"][0]["properties"] = {"not_a_property": 1}
    assert protocol.validate_payload(frame) != []


def test_missing_edit_rejects_modify_objects() -> None:
    """modify_objects needs `set` or `delta` (7.5)."""
    frame = {"type": "modify_objects", "id": "s-2", "payload": {"targets": [1, 2]}}
    assert protocol.validate_payload(frame) != []


def test_delta_may_only_move_and_rotate() -> None:
    frame = {
        "type": "modify_objects",
        "id": "s-2",
        "payload": {"targets": [1], "delta": {"x": 30, "groups": [1]}},
    }
    assert protocol.validate_payload(frame) != []
