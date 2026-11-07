"""GridText: format, legend, reading and writing (LEVEL-DESIGN-SPEC.md 5)."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gd_ai_assistant.design.coords import Grid
from gd_ai_assistant.design.grid import (
    BUILTIN_BY_CHAR,
    BUILTINS,
    CONTINUATION,
    CUSTOM_CHARS,
    EMPTY,
    UNKNOWN,
    GridBlock,
    GridError,
    emit,
    footprint,
    parse,
    parse_legend,
    plan_placement,
    render,
)
from gd_ai_assistant.design.level_model import LevelModel, LevelObject
from gd_ai_assistant.design.roles import Hitbox, Role, load_role_table

ROLES = load_role_table()
GRID = Grid(ground_top_y=0.0)
CELL_CHARS = EMPTY + CONTINUATION + UNKNOWN + "".join(BUILTIN_BY_CHAR) + CUSTOM_CHARS


# -- format --------------------------------------------------------------------------------


@st.composite
def grids(draw) -> str:
    w = draw(st.integers(1, 12))
    h = draw(st.integers(1, 6))
    col = draw(st.integers(-50, 500))
    row = draw(st.integers(-5, 20))
    rows = [draw(st.text(alphabet=CELL_CHARS, min_size=w, max_size=w)) for _ in range(h)]
    lines = [f"@grid col={col} row={row} w={w} h={h}"]
    for line in rows:
        if draw(st.booleans()) and draw(st.booleans()):
            lines.append(";" + draw(st.text(alphabet="abc xyz.#", max_size=10)))
        lines.append(line)
    return "\n".join(lines)


@given(grids())
@settings(max_examples=300)
def test_emit_parse_round_trip(text: str) -> None:
    """The M1 property: emit(parse(g)) == g for any valid canonical grid."""
    assert emit(parse(text)) == text


def test_trailing_newline_and_crlf_are_tolerated() -> None:
    text = "@grid col=0 row=0 w=2 h=1\n#^"
    assert emit(parse(text + "\n")) == text
    assert emit(parse(text.replace("\n", "\r\n"))) == text


def test_header_keys_in_any_order() -> None:
    assert parse("@grid h=1 w=1 row=2 col=3\n#") == GridBlock(3, 2, 1, 1, ("#",))


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "header"),
        ("@grid col=0 row=0 w=3\n...", "missing h"),
        ("@grid col=0 row=0 w=3 h=1\n..", "2 characters"),
        ("@grid col=0 row=0 w=3 h=2\n...", "1 rows"),
        ("@grid col=0 row=0 w=3 h=1\n.Q.", "unknown characters 'Q'"),
        ("@grid col=0 row=0 w=0 h=1\n", "at least 1"),
        ("grid col=0 row=0 w=1 h=1\n.", "bad header"),
    ],
)
def test_parse_errors_say_what_is_wrong(text: str, fragment: str) -> None:
    with pytest.raises(GridError) as caught:
        parse(text)
    assert fragment in str(caught.value)


def test_rows_are_top_first_and_row_is_the_bottom() -> None:
    block = parse("@grid col=10 row=2 w=2 h=2\n^.\n##")
    assert block.char_at(10, 3) == "^"
    assert block.char_at(11, 2) == "#"
    assert list(block.cells()) == [(10, 3, "^"), (10, 2, "#"), (11, 2, "#")]


# -- legend -------------------------------------------------------------------------------------


def test_every_builtin_default_id_has_its_role_in_roles_json() -> None:
    """5.2.1: every default id must exist in roles.json with the expected role."""
    for builtin in BUILTINS:
        entry = ROLES.entries.get(builtin.id)
        assert entry is not None, builtin
        assert entry.role is builtin.role, builtin
        if builtin.variant is not None:
            assert entry.variant == builtin.variant, builtin


def test_character_sets_do_not_overlap() -> None:
    specials = {EMPTY, CONTINUATION, UNKNOWN, ";"}
    assert not set(CUSTOM_CHARS) & set(BUILTIN_BY_CHAR)
    assert not set(CUSTOM_CHARS) & specials
    assert not set(BUILTIN_BY_CHAR) & specials
    assert len(set(CUSTOM_CHARS)) == len(CUSTOM_CHARS)


def test_custom_legend_validation() -> None:
    legend = parse_legend({"a": {"id": 1338, "rotation": 450},
                           "x": {"id": 211, "properties": {"color_channel_1": 4}}})
    assert legend["a"].rotation == 90
    with pytest.raises(GridError) as caught:
        parse_legend({"^": {"id": 9}, "Q": {"id": 1}, "a": {"id": 1, "rotation": 45},
                      "c": {"id": 1, "scale": 2}, "e": {"id": 1, "properties": {"x": 1}}})
    assert len(caught.value.problems) == 5


# -- footprints ---------------------------------------------------------------------------------


def test_footprints_of_real_objects() -> None:
    assert footprint(ROLES.get(8).hitbox) == ()            # spike
    assert footprint(ROLES.get(36).hitbox) == ()           # orb
    assert set(footprint(ROLES.get(12).hitbox)) == {(0, -1), (0, 1)}   # portal, 86 tall
    assert set(footprint(ROLES.get(12).hitbox, 90)) == {(-1, 0), (1, 0)}
    assert set(footprint(ROLES.get(88).hitbox)) == {(0, 1), (0, -1), (1, 0), (-1, 0)}


def test_rotation_turns_an_offset_box_clockwise() -> None:
    box = Hitbox(x=0, y=-5, w=70, h=10)           # reaches two cells to the right
    assert set(footprint(box)) == {(1, 0), (2, 0)}
    assert set(footprint(box, 90)) == {(0, -1), (0, -2)}   # clockwise: right becomes down


# -- writing -----------------------------------------------------------------------------------


def test_placement_payloads_sit_on_cell_centres() -> None:
    block = parse("@grid col=4 row=0 w=3 h=2\n.o.\n#>#")
    placement = plan_placement(block, {}, GRID, ROLES)
    assert placement.payloads == [
        {"id": 36, "x": 4 * 30 + 45.0, "y": 45.0},
        {"id": 1, "x": 135.0, "y": 15.0},
        {"id": 8, "x": 165.0, "y": 15.0, "properties": {"rotation": 90}},
        {"id": 1, "x": 195.0, "y": 15.0},
    ]
    assert placement.notes == []


def test_placement_rejects_what_it_cannot_write() -> None:
    block = parse("@grid col=0 row=0 w=5 h=3\n.....\n.?:a.\n.....")
    with pytest.raises(GridError) as caught:
        plan_placement(block, {}, GRID, ROLES)
    text = str(caught.value)
    assert "'?' cannot be written" in text
    assert "no object in this block covers it" in text
    assert "no legend entry" in text


def test_continuations_under_a_portal_are_accepted() -> None:
    block = parse("@grid col=0 row=0 w=3 h=5\n...\n.:.\n.S.\n.:.\n...")
    placement = plan_placement(block, {}, GRID, ROLES)
    assert [p["id"] for p in placement.payloads] == [13]


def test_continuation_on_the_border_may_belong_to_a_neighbour() -> None:
    block = parse("@grid col=0 row=0 w=2 h=1\n:#")
    assert len(plan_placement(block, {}, GRID, ROLES).payloads) == 1


def test_slabs_sit_at_the_top_or_bottom_of_their_cell() -> None:
    """S-L9: the editor puts a plank at the top of its cell (+8); '=' is the bottom (-8)."""
    placement = plan_placement(parse("@grid col=0 row=0 w=2 h=1\n-="), {}, GRID, ROLES)
    assert [p["y"] for p in placement.payloads] == [23.0, 7.0]
    assert placement.notes == []


def test_an_editor_placed_plank_reads_as_a_top_slab() -> None:
    model = _model([{"id": 40, "x": 15, "y": 23}, {"id": 40, "x": 45, "y": 7}])
    assert render(model, GRID, col=0, row=0, w=2, h=1).block.rows == ("-=",)


# -- reading ----------------------------------------------------------------------------------


def _model(payloads: list[dict[str, Any]]) -> LevelModel:
    records = []
    for uid, payload in enumerate(payloads, start=100):
        props = {"x": payload["x"], "y": payload["y"], **payload.get("properties", {})}
        records.append({"object_id": uid, "id": payload["id"], "properties": props,
                        "save": ""})
    return LevelModel([LevelObject.from_record(r, ROLES) for r in records], revision=1)


def _round_trip(text: str, legend: dict | None = None) -> tuple[str, Any]:
    block = parse(text)
    placement = plan_placement(block, parse_legend(legend), GRID, ROLES)
    rendered = render(_model(placement.payloads), GRID, col=block.col, row=block.row,
                      w=block.w, h=block.h)
    return emit(rendered.block), rendered


SECTION = """@grid col=120 row=0 w=24 h=6
.........:..............
.........S....##........
.........:....##....^^..
....^.........##...####.
........................
########################"""


def test_read_write_read_is_stable() -> None:
    """The M1 fixture property: a written grid reads back as the same text."""
    text, rendered = _round_trip(SECTION)
    assert text == SECTION
    assert rendered.extras == [] and rendered.overlaps == []
    assert rendered.legend_used["#"]["count"] == 24 + 6 + 4


def test_custom_characters_round_trip_through_their_legend() -> None:
    text = "@grid col=0 row=0 w=3 h=1\na#c"
    legend = {"a": {"id": 1, "rotation": 90}, "c": {"id": 8, "flip_x": True}}
    first, rendered = _round_trip(text, legend)
    # Reading assigns its own characters; feeding them back reproduces the grid.
    second, _ = _round_trip(first, rendered.custom_legend)
    assert second == first
    assert len(rendered.custom_legend) == 2


def test_off_centre_rotated_and_scaled_objects_are_shown_as_unknown() -> None:
    model = _model([
        {"id": 1, "x": 15 + 5, "y": 15},                                  # 5 units off
        {"id": 1, "x": 45, "y": 15, "properties": {"rotation": 45}},
        {"id": 1, "x": 75, "y": 15, "properties": {"scale_x": 2.0}},
        {"id": 1, "x": 105 + 2.5, "y": 15},                               # within tolerance
    ])
    rendered = render(model, GRID, col=0, row=0, w=4, h=1)
    assert emit(rendered.block).splitlines()[1] == "???#"
    reasons = [e["reason"] for e in rendered.extras]
    assert any("off its cell centre" in r for r in reasons)
    assert any("multiple of 90" in r for r in reasons)
    assert any("scaled" in r for r in reasons)


def test_overlap_shows_the_stronger_role_and_is_reported() -> None:
    model = _model([{"id": 1, "x": 15, "y": 15}, {"id": 8, "x": 15, "y": 15}])
    rendered = render(model, GRID, col=0, row=0, w=1, h=1)
    assert rendered.block.rows == ("^",)
    assert rendered.overlaps == [{"cell": [0, 0], "uids": [101, 100]}]


def test_hidden_layers_are_counted_in_notes() -> None:
    deco = next(i for i, e in ROLES.entries.items() if e.role is Role.DECO)
    model = _model([{"id": deco, "x": 15, "y": 15}, {"id": 1, "x": 45, "y": 15}])
    rendered = render(model, GRID, col=0, row=0, w=2, h=1)
    assert rendered.block.rows == (".#",)
    assert any("1 deco objects hidden" in n for n in rendered.notes)


def test_portal_anchor_outside_the_window_leaves_continuations() -> None:
    model = _model([{"id": 13, "x": 15, "y": 45}])          # ship portal at row 1
    rendered = render(model, GRID, col=0, row=2, w=1, h=2)
    assert rendered.block.rows == (".", ":")


def test_the_prompt_lists_every_writable_builtin_character() -> None:
    """A prompt legend that disagrees with the code teaches the model wrong characters."""
    from gd_ai_assistant.agent_prompt import system_prompt

    prompt = system_prompt()
    bullet = prompt[prompt.index("**Characters:**"):prompt.index("**Tall objects")]
    for builtin in BUILTINS:
        assert builtin.char in bullet, builtin


def test_measured_snap_offsets_put_objects_on_the_floor() -> None:
    """S-L9: what the editor's own placement measured (scripts/check_snap.py)."""
    placement = plan_placement(parse("@grid col=0 row=0 w=4 h=1\n,_~!"), {}, GRID, ROLES)
    assert [p["y"] for p in placement.payloads] == [6.0, 2.0, 2.0, 3.0]
    assert placement.notes == []


def test_editor_placed_pads_read_back_as_their_characters() -> None:
    """A pad the user placed by hand (y = 2) is '_', not an off-centre '?'."""
    model = _model([{"id": 35, "x": 15, "y": 2}, {"id": 39, "x": 45, "y": 6},
                    {"id": 67, "x": 75, "y": 3}])
    rendered = render(model, GRID, col=0, row=0, w=3, h=1)
    assert rendered.block.rows == ("_,!",)
    assert rendered.extras == []


def test_aligned_custom_blocks_round_trip() -> None:
    """Any half block the editor places at the top of its cell reads back exactly."""
    model = _model([{"id": 662, "x": 15, "y": 22.5}, {"id": 147, "x": 45, "y": 7}])
    rendered = render(model, GRID, col=0, row=0, w=2, h=1)
    assert UNKNOWN not in rendered.block.rows[0]
    legend = rendered.custom_legend
    assert sorted(e["align"] for e in legend.values()) == ["bottom", "top"]
    placement = plan_placement(rendered.block, parse_legend(legend), GRID, ROLES)
    assert [(p["id"], p["y"]) for p in placement.payloads] == [(662, 22.5), (147, 7.0)]


def test_align_offsets_match_what_s_l9_measured() -> None:
    from gd_ai_assistant.design.grid import Glyph, align_offset

    plank = ROLES.get(40).hitbox
    assert align_offset(Glyph(40, align="top"), plank) == (0.0, 8.0)
    assert align_offset(Glyph(40, align="bottom"), plank) == (0.0, -8.0)
    assert align_offset(Glyph(662, align="top"), ROLES.get(662).hitbox) == (0.0, 7.5)
    # A plank turned on its side is flush left/right instead.
    assert align_offset(Glyph(40, rotation=90, align="left"), plank) == (-8.0, 0.0)


def test_align_is_validated() -> None:
    with pytest.raises(GridError):
        parse_legend({"a": {"id": 1, "align": "middle"}})


def test_an_object_half_a_cell_off_is_unknown_not_aligned() -> None:
    """Seen in a real level: spikes at x = 30c (cell boundaries) must show as '?'."""
    rendered = render(_model([{"id": 8, "x": 330, "y": 15}]), GRID, col=10, row=0, w=2, h=1)
    assert UNKNOWN in rendered.block.rows[0]
    assert rendered.custom_legend == {}
