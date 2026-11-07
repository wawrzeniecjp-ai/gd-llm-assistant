"""GridText and schematic tools in real turns against the fake editor (M1)."""

from __future__ import annotations

import json

from test_design_agent import ScriptProvider, call, make_loop, text

from evals.fake_editor import FakeEditor
from gd_ai_assistant.agent import COMPLETED

SECTION = """@grid col=4 row=0 w=8 h=4
........
.....##.
..o..##^
####.###"""


def _tool_result(provider: ScriptProvider, index: int = 0) -> dict:
    tool_messages = [m for m in provider.seen_messages if m.role == "tool"]
    return json.loads(tool_messages[index].text)


async def test_place_then_render_reproduces_the_grid() -> None:
    editor = FakeEditor()
    provider = ScriptProvider([
        call("place_grid", {"grid": SECTION}),
        call("render_grid", {"cols": [4, 11], "rows": [0, 3]}, "c2"),
        text(),
    ])
    result = await make_loop(editor, provider).run(turn_id="t1", message_id="m1", user_text="go")

    assert result.status == COMPLETED
    placed, rendered = _tool_result(provider, 0), _tool_result(provider, 1)
    assert placed["created"] == 13 and placed["deleted"] == 0
    assert rendered["grid"] == SECTION
    assert len(editor.objects) == 13
    # One transaction around the edit, like any other mutating turn.
    types = [t for t, _ in editor.requests]
    assert types.index("begin_transaction") < types.index("create_objects")
    assert [a.kind for a in result.actions] == ["create_objects"]


async def test_add_refuses_occupied_cells_and_replace_rebuilds() -> None:
    editor = FakeEditor()
    editor.add(1, 4 * 30 + 15, 15)          # a block in cell (4, 0)
    deco = 211                                  # deco survives a replace
    editor.add(deco, 5 * 30 + 15, 15)
    block = "@grid col=4 row=0 w=2 h=1\n^^"
    provider = ScriptProvider([
        call("place_grid", {"grid": block}),
        call("place_grid", {"grid": block, "mode": "replace"}, "c2"),
        text(),
    ])
    await make_loop(editor, provider).run(turn_id="t1", message_id="m1", user_text="go")

    refused = _tool_result(provider, 0)
    assert "already hold gameplay" in refused["error"]
    assert refused["clashes"][0]["cell"] == [4, 0]
    replaced = _tool_result(provider, 1)
    assert replaced["deleted"] == 1 and replaced["created"] == 2
    assert sorted(o.type_id for o in editor.objects.values()) == [8, 8, deco]


async def test_preview_changes_nothing() -> None:
    editor = FakeEditor()
    provider = ScriptProvider([call("place_grid", {"grid": SECTION, "preview": True}), text()])
    await make_loop(editor, provider).run(turn_id="t1", message_id="m1", user_text="go")
    preview = _tool_result(provider)
    assert preview["would_create"] == 13
    assert editor.objects == {}
    assert "begin_transaction" not in [t for t, _ in editor.requests]


async def test_bad_grid_lists_every_problem() -> None:
    editor = FakeEditor()
    bad = "@grid col=0 row=0 w=3 h=1\n?Za"
    provider = ScriptProvider([call("place_grid", {"grid": bad}), text()])
    await make_loop(editor, provider).run(turn_id="t1", message_id="m1", user_text="go")
    result = _tool_result(provider)
    assert "nothing was placed" in result["error"]
    assert result["problems"]
    assert editor.objects == {}


async def test_place_grid_respects_the_turn_object_cap() -> None:
    editor = FakeEditor()
    wide = "@grid col=0 row=0 w=40 h=1\n" + "#" * 40
    loop = make_loop(editor, ScriptProvider([call("place_grid", {"grid": wide}), text()]))
    loop._max_objects = 10
    await loop.run(turn_id="t1", message_id="m1", user_text="go")
    assert editor.objects == {}


async def test_render_schematic_attaches_an_image() -> None:
    editor = FakeEditor()
    provider = ScriptProvider([
        call("place_grid", {"grid": SECTION}),
        call("render_schematic", {"x_range": [0, 600]}, "c2"),
        text(),
    ])
    await make_loop(editor, provider).run(turn_id="t1", message_id="m1", user_text="look")
    described = _tool_result(provider, 1)
    assert described["drawn"]["solid"] == 11
    assert described["drawn"]["hazard"] == 1 and described["drawn"]["orb"] == 1
    images = [m for m in provider.seen_messages if m.images]
    assert images and images[-1].images[0].startswith("data:image/png;base64,")


async def test_schematic_reports_later_overlays_as_unavailable() -> None:
    editor = FakeEditor()
    provider = ScriptProvider([
        call("render_schematic", {"x_range": [0, 300], "overlays": ["grid", "route"]}),
        text(),
    ])
    await make_loop(editor, provider).run(turn_id="t1", message_id="m1", user_text="look")
    assert any("M2" in note for note in _tool_result(provider)["notes"])
