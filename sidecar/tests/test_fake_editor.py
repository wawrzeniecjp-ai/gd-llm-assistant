"""The fake editor stays inside the protocol (LEVEL-DESIGN-SPEC.md 15.2).

Each call goes through schema validation in both directions, so these tests are mostly
"does every handler produce a valid result for a valid request". The behaviour checks
are the ones other tests will lean on: transactions, revisions, and refusal codes.
"""

from __future__ import annotations

import pytest

from evals.fake_editor import FakeEditor
from gd_ai_assistant import protocol


async def _ok(editor: FakeEditor, type_name: str, payload: dict | None = None) -> dict:
    response = await editor.request(type_name, payload or {})
    assert response.ok, response.error
    return response.result


async def test_every_handler_answers_inside_the_schema() -> None:
    editor = FakeEditor(
        describe={1: {"id": 1, "object_type": 0, "hitbox": {"x": -15, "y": -15, "w": 30, "h": 30},
                      "radius": 0, "is_trigger": False, "is_decoration": False,
                      "has_detail_color": False, "default_z_layer": 0}},
        catalog={1: "square_01_001.png", 8: "spike_01_001.png", 899: None},
    )
    a = editor.add(1, 15, 15)
    b = editor.add(8, 45, 15, rotation=90.0)

    await _ok(editor, "ping")
    await _ok(editor, "get_editor_state")
    await _ok(editor, "query_objects", {"filters": {"x_range": [0, 100]}, "limit": 1})
    await _ok(editor, "summarize_region", {"x_range": [0, 100]})
    await _ok(editor, "get_object_properties", {"object_id": a})
    await _ok(editor, "get_save_properties", {"object_id": b})
    await _ok(editor, "list_object_catalog", {"offset": 0, "limit": 2})
    await _ok(editor, "describe_object_types", {"offset": 0, "limit": 10})
    await _ok(editor, "describe_object_types", {"ids": [1, 8]})
    await _ok(editor, "get_level_objects", {"limit": 1})
    await _ok(editor, "get_camera")
    await _ok(editor, "set_camera", {"x": 100, "zoom": 9})
    await _ok(editor, "get_level_settings")
    await _ok(editor, "get_color_channel", {"channel": 1})
    await _ok(editor, "set_color_channel", {"channel": 1, "red": 10, "green": 20, "blue": 30})
    await _ok(editor, "set_selection", {"targets": [a]})
    txn = (await _ok(editor, "begin_transaction", {"label": "t"}))["txn_id"]
    created = await _ok(editor, "create_objects", {"objects": [{"id": 1, "x": 75, "y": 15}]})
    await _ok(editor, "modify_objects", {"targets": [a], "delta": {"x": 30}})
    await _ok(editor, "set_save_property", {"targets": created["created"], "key": 10, "value": "1"})
    await _ok(editor, "delete_objects", {"targets": [b, 999]})
    summary = await _ok(editor, "end_transaction", {"txn_id": txn})
    assert summary["summary"] == {"created": 1, "modified": 2, "deleted": 1}
    await _ok(editor, "revert_transaction", {"txn_id": txn})
    assert set(editor.objects) == {a, b}


async def test_invalid_payload_is_refused_like_the_mod() -> None:
    editor = FakeEditor()
    response = await editor.request("create_objects", {"objects": [{"id": 1}]})
    assert not response.ok and response.code == "INVALID_REQUEST"


async def test_unknown_and_unsupported_requests() -> None:
    editor = FakeEditor(unsupported={"describe_object_types"})
    assert (await editor.request("teleport_player")).code == "UNSUPPORTED_REQUEST"
    assert (await editor.request("describe_object_types")).code == "UNSUPPORTED_REQUEST"


async def test_revision_moves_on_every_mutation_and_hand_edit() -> None:
    editor = FakeEditor()
    seen = [editor.revision]

    async def state() -> int:
        return (await _ok(editor, "get_editor_state"))["revision"]

    uid = editor.add(1, 15, 15)
    seen.append(await state())
    await _ok(editor, "modify_objects", {"targets": [uid], "set": {"rotation": 90}})
    seen.append(await state())
    editor.hand_edit()
    seen.append(await state())
    await _ok(editor, "get_level_objects")
    seen.append(await state())
    assert seen[:4] == sorted(set(seen[:4]))  # strictly increasing
    assert seen[3] == seen[4]  # a read does not move it


async def test_cursor_from_another_revision_is_refused() -> None:
    editor = FakeEditor()
    for i in range(3):
        editor.add(1, 15 + 30 * i, 15)
    first = await _ok(editor, "get_level_objects", {"limit": 1})
    editor.hand_edit()
    response = await editor.request("get_level_objects", {"cursor": first["next_cursor"]})
    assert response.code == "LEVEL_CHANGING"


async def test_second_transaction_is_refused_while_one_is_open() -> None:
    editor = FakeEditor()
    await _ok(editor, "begin_transaction", {"label": "a"})
    assert (await editor.request("begin_transaction", {"label": "b"})).code == "TRANSACTION_ACTIVE"


@pytest.mark.parametrize("type_name", ["get_level_objects", "describe_object_types"])
def test_new_requests_are_registered(type_name: str) -> None:
    spec = protocol.spec_for(type_name)
    assert spec is not None and spec.is_request and spec.direction == "s2m"
