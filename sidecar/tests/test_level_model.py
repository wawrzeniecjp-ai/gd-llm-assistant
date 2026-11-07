"""The level snapshot and the fake editor behind it (LEVEL-DESIGN-SPEC.md 4.3, 15.2)."""

from __future__ import annotations

import pytest

from evals.fake_editor import FakeEditor
from gd_ai_assistant.design.level_model import (
    LevelChangingError,
    LevelModel,
    SnapshotCache,
    SnapshotError,
    load_level_model,
    parse_save_string,
)
from gd_ai_assistant.design.roles import Role, RoleTable, role_from_description


def _roles() -> RoleTable:
    def entry(object_id: int, object_type: int, **flags):
        return role_from_description({
            "id": object_id, "object_type": object_type,
            "hitbox": {"x": -15, "y": -15, "w": 30, "h": 30}, "radius": 0,
            "is_trigger": False, "is_decoration": False, "has_detail_color": False,
            "default_z_layer": 0, **flags,
        })
    return RoleTable(entries={
        1: entry(1, 0),
        8: entry(8, 2),
        36: entry(36, 11),
        211: entry(211, 7),
        899: entry(899, 0, is_trigger=True),
    })


def _editor(count: int = 0) -> FakeEditor:
    editor = FakeEditor()
    for i in range(count):
        editor.add(1, 15 + 30 * i, 15)
    return editor


# -- parsing -------------------------------------------------------------------


def test_parse_save_string() -> None:
    parsed = parse_save_string("1,899,2,165,3,195,10,0.5")
    assert parsed == {1: "899", 2: "165", 3: "195", 10: "0.5"}


def test_parse_save_string_skips_malformed_pairs() -> None:
    assert parse_save_string("1,8,x,9,2") == {1: "8"}
    assert parse_save_string("") == {}


# -- loading -------------------------------------------------------------------


async def test_snapshot_reads_every_page_at_one_revision() -> None:
    editor = _editor(25)
    model = await load_level_model(editor, _roles(), page_size=10)
    assert len(model) == 25
    assert model.revision == editor.revision
    pages = [p for t, p in editor.requests if t == "get_level_objects"]
    assert len(pages) == 3
    assert "cursor" not in pages[0] and "cursor" in pages[1]


async def test_objects_carry_named_properties_and_raw_keys() -> None:
    editor = _editor()
    uid = editor.add(899, 165, 195, rotation=90.0, groups=[4, 7], extra={10: "0.5"})
    model = await load_level_model(editor, _roles())
    obj = model.by_uid(uid)
    assert obj is not None
    assert (obj.x, obj.y, obj.rotation) == (165.0, 195.0, 90.0)
    assert obj.groups == (4, 7)
    assert obj.raw[10] == "0.5"
    assert obj.role is Role.TRIGGER
    assert obj.rotation_is_axis_aligned


async def test_a_hand_edit_between_pages_restarts_the_read() -> None:
    editor = _editor(25)
    edits = {"done": False}

    def edit_once(type_name, payload):
        if type_name == "get_level_objects" and "cursor" in payload and not edits["done"]:
            edits["done"] = True
            editor.add(8, 900, 15)

    editor.before_request = edit_once
    model = await load_level_model(editor, _roles(), page_size=10)
    # The second attempt sees the new object; nothing from the first attempt leaks in.
    assert len(model) == 26
    assert len({o.uid for o in model}) == 26


async def test_a_level_that_keeps_changing_is_reported() -> None:
    editor = _editor(25)

    def always_edit(type_name, payload):
        if type_name == "get_level_objects" and "cursor" in payload:
            editor.hand_edit()

    editor.before_request = always_edit
    with pytest.raises(LevelChangingError):
        await load_level_model(editor, _roles(), page_size=10)


async def test_an_older_mod_is_a_snapshot_error_with_its_code() -> None:
    editor = FakeEditor(unsupported={"get_level_objects"})
    with pytest.raises(SnapshotError) as raised:
        await load_level_model(editor, _roles())
    assert raised.value.code == "UNSUPPORTED_REQUEST"


async def test_empty_level() -> None:
    model = await load_level_model(_editor(), _roles())
    assert len(model) == 0
    assert model.bounds() is None


# -- queries --------------------------------------------------------------------


async def test_in_rect_uses_centres_and_filters_by_role() -> None:
    editor = _editor()
    block = editor.add(1, 15, 15)
    spike = editor.add(8, 315, 15)
    editor.add(211, 320, 60)
    far = editor.add(1, 9000, 15)
    model = await load_level_model(editor, _roles())

    hits = model.in_rect(0, 0, 330, 30)
    assert [o.uid for o in hits] == [block, spike]
    assert [o.uid for o in model.in_rect(0, 0, 400, 100, roles=[Role.DECO])] != []
    assert [o.uid for o in model.in_rect(330, 30, 0, 0, roles=[Role.HAZARD])] == [spike]
    assert model.in_rect(8990, 0, 9010, 30)[0].uid == far


async def test_gameplay_excludes_deco_and_triggers() -> None:
    editor = _editor()
    editor.add(1, 15, 15)
    editor.add(211, 15, 45)
    editor.add(899, 15, 300)
    model = await load_level_model(editor, _roles())
    assert [o.type_id for o in model.gameplay()] == [1]


async def test_unknown_ids_fall_back_to_heuristics_and_are_listed() -> None:
    editor = _editor()
    editor.add(4242, 15, 15)
    model = await load_level_model(editor, _roles().with_frames({4242: "spike_09_001.png"}))
    assert model.objects[0].role is Role.HAZARD
    assert [o.type_id for o in model.heuristic_roles()] == [4242]


def test_rotation_alignment() -> None:
    roles = _roles()
    model = LevelModel([], revision=0)
    assert len(model) == 0
    from gd_ai_assistant.design.level_model import LevelObject

    def obj(rotation: float) -> LevelObject:
        return LevelObject.from_record(
            {"object_id": 1, "id": 1, "properties": {"x": 0, "y": 0, "rotation": rotation},
             "save": "1,1"},
            roles,
        )

    assert obj(0).rotation_is_axis_aligned
    assert obj(270).rotation_is_axis_aligned
    assert obj(-90).rotation_is_axis_aligned
    assert not obj(30).rotation_is_axis_aligned


# -- cache ------------------------------------------------------------------------


async def test_cache_reuses_until_invalidated() -> None:
    editor = _editor(3)
    cache = SnapshotCache(editor, _roles())
    first = await cache.get()
    assert await cache.get() is first
    assert cache.loads == 1
    cache.invalidate()
    editor.add(1, 500, 15)
    second = await cache.get()
    assert len(second) == 4 and cache.loads == 2
