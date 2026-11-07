"""Design layer foundations: constants, coordinates, roles (LEVEL-DESIGN-SPEC.md 4.1, 4.2)."""

from __future__ import annotations

import json

import pytest

from gd_ai_assistant.design import constants as constants_module
from gd_ai_assistant.design.constants import Measured, PhysicsConstants, load_constants
from gd_ai_assistant.design.coords import CELL, Cell, Grid, GroundUnknownError
from gd_ai_assistant.design.roles import (
    GAMEPLAY_ROLES,
    Role,
    RoleTable,
    load_role_table,
    role_from_description,
    role_from_frame,
)

# -- constants ---------------------------------------------------------------


def test_packaged_constants_load_and_carry_provenance() -> None:
    constants = load_constants()
    assert constants.game_version
    for speed, value in constants.units_per_second.items():
        assert value.source, speed
        # Nothing is calibrated yet; a guess must never pass as a measurement.
        assert not value.calibrated or value.confidence != "guess"


def test_ground_is_the_measured_s_l1_value() -> None:
    """S-L1 measured the ground top at y = 0; it must stay a measurement, not a guess."""
    ground = load_constants().ground_top_y
    assert ground.value == 0.0
    assert ground.calibrated


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"value": 1.0, "source": "x"},
        {"value": 1.0, "confidence": "measured"},
        {"value": "1", "source": "x", "confidence": "measured"},
        {"value": 1.0, "source": "x", "confidence": "certain"},
    ],
)
def test_measured_rejects_incomplete_provenance(raw) -> None:
    with pytest.raises(ValueError):
        Measured.from_json(raw, where="test")


def test_guess_is_known_but_not_calibrated() -> None:
    guess = Measured(1.0, "memory", "guess")
    assert guess.known and not guess.calibrated
    assert Measured(1.0, "probe", "measured").calibrated


def _constants(ground: float | None) -> PhysicsConstants:
    raw = json.loads(constants_module._default_path().read_text(encoding="utf-8"))
    raw["ground_top_y"] = {"value": ground, "source": "test", "confidence": "measured"}
    return PhysicsConstants.from_json(raw)


# -- coordinates ---------------------------------------------------------------


def test_grid_refuses_without_a_measured_ground() -> None:
    with pytest.raises(GroundUnknownError):
        Grid.from_constants(_constants(None))


def test_row_zero_sits_on_the_ground() -> None:
    grid = Grid.from_constants(_constants(90.0))
    assert grid.centre(Cell(0, 0)) == (CELL / 2, 90.0 + CELL / 2)
    assert grid.cell_of(15.0, 105.0) == Cell(0, 0)
    # Just below the ground top is row -1, inside the ground.
    assert grid.row_of(89.9) == -1
    assert grid.cell_of(-0.1, 90.0) == Cell(-1, 0)


def test_cell_round_trip() -> None:
    grid = Grid(ground_top_y=0.0)
    for col in range(-3, 4):
        for row in range(-2, 5):
            assert grid.cell_of(*grid.centre(Cell(col, row))) == Cell(col, row)


def test_offset_from_centre() -> None:
    grid = Grid(ground_top_y=0.0)
    assert grid.offset_from_centre(17.0, 14.0) == (2.0, -1.0)


# -- roles -------------------------------------------------------------------------


def _description(object_type: int, **flags) -> dict:
    return {
        "id": 100,
        "object_type": object_type,
        "hitbox": {"x": -15.0, "y": -15.0, "w": 30.0, "h": 30.0},
        "radius": 0.0,
        "visual": {"w": 30.0, "h": 30.0},
        "is_trigger": False,
        "is_decoration": False,
        "has_detail_color": False,
        "default_z_layer": 0,
        **flags,
    }


@pytest.mark.parametrize(
    ("object_type", "role", "variant"),
    [
        (0, Role.SOLID, None),
        (2, Role.HAZARD, None),
        (5, Role.PORTAL_MODE, "ship"),
        (8, Role.PAD, "yellow"),
        (12, Role.ORB, "pink"),
        (18, Role.PORTAL_SIZE, "mini"),
        (3, Role.PORTAL_GRAVITY, "inverse"),
        (7, Role.DECO, None),
        (25, Role.SLOPE, None),
        (41, Role.PORTAL_MODE, "swing"),
    ],
)
def test_game_types_map_to_roles(object_type, role, variant) -> None:
    entry = role_from_description(_description(object_type))
    assert entry.role is role
    assert entry.variant == variant
    assert entry.source == "game"


def test_trigger_flag_wins_over_type() -> None:
    assert role_from_description(_description(0, is_trigger=True)).role is Role.TRIGGER


def test_decoration_flag_overrides_the_solid_default() -> None:
    """Type 0 is the enum default, so a deco object that never set a type reads as Solid."""
    assert role_from_description(_description(0, is_decoration=True)).role is Role.DECO


def test_decoration_flag_does_not_hide_a_hazard() -> None:
    assert role_from_description(_description(2, is_decoration=True)).role is Role.HAZARD


@pytest.mark.parametrize(
    ("object_id", "role", "variant"),
    [(200, Role.PORTAL_SPEED, "0.5x"), (1334, Role.PORTAL_SPEED, "4x"),
     (2016, Role.DECO, "camera"), (2063, Role.SPECIAL, None)],
)
def test_modifiers_are_told_apart_by_id(object_id, role, variant) -> None:
    entry = role_from_description({**_description(20), "id": object_id})
    assert entry.role is role and entry.variant == variant


def test_modifier_id_override_needs_the_modifier_type() -> None:
    """If a future version renumbers id 200, it must not inherit the speed-portal label."""
    assert role_from_description({**_description(0), "id": 200}).role is Role.SOLID


def test_speed_portal_variants_are_known_speeds() -> None:
    from gd_ai_assistant.design.constants import SPEEDS
    from gd_ai_assistant.design.roles import _MODIFIER_IDS

    speeds = {v for r, _, v in _MODIFIER_IDS.values() if r is Role.PORTAL_SPEED}
    assert speeds == set(SPEEDS)


def test_unmapped_type_is_special_not_deco() -> None:
    """An unknown type must be something the simulator refuses to judge, never ignores."""
    assert role_from_description(_description(999)).role is Role.SPECIAL


def test_circular_hitbox_keeps_its_radius() -> None:
    entry = role_from_description(_description(2, radius=12.5))
    assert entry.hitbox is not None and entry.hitbox.is_circle


def test_heuristic_fallback_is_marked() -> None:
    entry = role_from_frame(8, "spike_01_001.png")
    assert entry.role is Role.HAZARD and entry.heuristic
    assert role_from_frame(9999, None).role is Role.SPECIAL


def test_table_prefers_game_entries_and_falls_back_with_frames() -> None:
    game = role_from_description(_description(0))
    table = RoleTable(entries={100: game}).with_frames({8: "spike_01_001.png"})
    assert table.get(100) is game
    assert table.role_of(8) is Role.HAZARD
    assert table.get(8).heuristic


def test_role_table_json_round_trip(tmp_path) -> None:
    entries = {
        1: role_from_description({**_description(0), "id": 1}),
        2: role_from_description({**_description(2, radius=4.0), "id": 2}, frame="saw.png"),
    }
    table = RoleTable(entries=entries, game_version="2.2091")
    path = tmp_path / "roles.json"
    path.write_text(json.dumps(table.to_json()), encoding="utf-8")
    loaded = load_role_table(str(path))
    assert loaded.game_version == "2.2091"
    assert loaded.entries == entries


def test_missing_role_table_is_empty_not_an_error(tmp_path) -> None:
    table = load_role_table(str(tmp_path / "absent.json"))
    assert len(table) == 0
    assert table.get(8).heuristic


def test_gameplay_roles_exclude_deco_and_triggers() -> None:
    assert Role.DECO not in GAMEPLAY_ROLES
    assert Role.TRIGGER not in GAMEPLAY_ROLES
    assert Role.HAZARD in GAMEPLAY_ROLES


async def test_fetch_role_table_from_the_fake_editor() -> None:
    from evals.fake_editor import FakeEditor
    from gd_ai_assistant.design.roles import fetch_role_table

    describe = {
        object_id: {**_description(object_type), "id": object_id}
        for object_id, object_type in [(1, 0), (8, 2), (36, 11)]
    }
    editor = FakeEditor(
        describe=describe,
        catalog={1: "square_01_001.png", 8: "spike_01_001.png", 36: "ring_01_001.png", 5: None},
    )
    seen: list[tuple[int, int]] = []
    table, failed = await fetch_role_table(
        editor, page_size=2, game_version="test", progress=lambda d, t: seen.append((d, t))
    )
    assert {i: e.role for i, e in table.entries.items()} == {
        1: Role.SOLID, 8: Role.HAZARD, 36: Role.ORB,
    }
    assert table.entries[8].frame == "spike_01_001.png"
    assert failed == [5]
    assert seen[-1] == (4, 4)
