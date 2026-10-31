"""The save-string property table (SPEC.md 7.5.3).

These tests exist for one reason: **the table must agree with the game**. It is
generated from the bindings rather than written, so the risk is not typos, it is a
generator that quietly parses the wrong thing -- which is exactly what happened during
development. The first version required `//` before every key, so a multi-key
annotation like `// property 28, property 29` yielded only 28, and every `CCPoint`
member lost half of itself.

So the assertions below are cross-checks against keys measured directly in the editor,
not against the generator's own output. If the generator drifts, these fail.
"""

from __future__ import annotations

from gd_ai_assistant import properties_table as pt


def test_the_table_loads_and_records_which_game_version_it_came_from() -> None:
    table = pt.load_table()
    assert table.gd_version != "missing", "run scripts/gen_properties.py"
    assert table.class_names, "no classes parsed"
    assert table.key_count > 400, f"only {table.key_count} keys; parsing probably broke"


def test_the_table_agrees_with_keys_measured_in_the_editor() -> None:
    """Every key here was observed in a real save string during this project.

    This is the assertion that matters. A generator that silently stopped parsing would
    still produce a plausible-looking file; it would not produce these numbers.
    """
    table = pt.load_table()
    measured = {
        1: "objectID",
        2: "m_positionX",
        3: "m_positionY",
        24: "zLayer",
        25: "zOrder",
        51: "targetGroupID",
        57: "groups",
        128: "scaleX",
        129: "scaleY",
        155: "mainColorKeyIndex",
        156: "detailColorKeyIndex",
    }
    for key, expected in measured.items():
        assert table.name_for_key(key) == expected, (
            f"key {key} should mean {expected!r} but the table says "
            f"{table.name_for_key(key)!r}"
        )


def test_x_and_y_come_from_the_supplement_because_the_bindings_omit_them() -> None:
    """The bindings do not annotate position at all, so it is measured instead.

    Worth a test of its own: it is the one place the table is not purely generated, and
    the provenance is recorded in the file so a reader can tell.
    """
    table = pt.load_table()
    assert set(table.supplement["keys"]) == {"2", "3"}
    assert "measured" in table.supplement["source"].lower()


def test_multi_key_annotations_produce_both_keys() -> None:
    """The bug that cost an afternoon: `// property 28, property 29` above one CCPoint.

    Requiring `//` before each key matched only the first, so `m_moveOffsetY` vanished.
    """
    table = pt.load_table()
    # 28/29 are the move offset X/Y pair, from a single annotation.
    assert table.name_for_key(28) == "moveOffsetX"
    assert table.name_for_key(29) == "moveOffsetY"


def test_inherited_properties_resolve_through_the_class_chain() -> None:
    """A trigger is an `EffectGameObject`, which is an `EnhancedGameObject`, ... a
    `GameObject`. Its scale and colour keys come from the root, and a model asking
    "what can I set on a trigger" must be told about them."""
    table = pt.load_table()
    assert table.has_class("EffectGameObject")
    parent = table._classes["EffectGameObject"]["parent"]
    assert parent is not None, "EffectGameObject should have a parent in the bindings"

    keys = {entry.key for entry in table.properties_for_class("EffectGameObject")}
    assert 128 in keys, "inherited scale_x missing"
    assert 155 in keys, "inherited colour index missing"

    own = {entry.key for entry in table.properties_for_class("RingObject")}
    assert own != keys, "two different classes reported identical property sets"


def test_search_by_name_finds_a_property_by_a_fragment() -> None:
    """Members are C++ field names, so nobody searches for `m_duration`."""
    table = pt.load_table()
    found = table.search_by_name("duration")
    names = {entry.name for entry in found}
    assert "duration" in names
    # A leading m_ is how the field is spelled, not how it is asked for.
    assert table.search_by_name("m_duration")[0].name in names


def test_the_tool_reports_an_unknown_key_as_unknown_rather_than_guessing() -> None:
    """An unannotated key must say so. The prompt tells the model to admit ignorance;
    the tool has to give it the chance."""
    result = pt.search_properties({"key": 999999})
    assert result["known"] is False
    assert result["name"] is None
    assert "not annotated" in result["note"]


def test_the_tool_answers_a_key_lookup() -> None:
    result = pt.search_properties({"key": 51})
    assert result["known"] is True
    assert result["name"] == "targetGroupID"


def test_the_tool_answers_a_name_lookup_and_says_how_wide_it_searched() -> None:
    result = pt.search_properties({"query": "easing"})
    assert result["matches"], "no property matched 'easing'"
    # The mod does not report object classes, so a model will not have one to pass.
    assert result["searched_all_classes"] is True
    assert result["table"]["gd_version"] != "missing"


def test_a_name_that_matches_nothing_explains_itself() -> None:
    result = pt.search_properties({"query": "zzzz-not-a-property"})
    assert result["matches"] == []
    assert "C++ field names" in result["note"]


def test_describe_class_lists_properties_and_rejects_an_unknown_class() -> None:
    described = pt.describe_class({"class": "GameObject"})
    assert described["property_count"] > 10
    assert described["properties"]
    assert "property_count" in described

    missing = pt.describe_class({"class": "NoSuchClass"})
    assert "error" in missing
    assert missing["classes_available"], "an unknown class should list what does exist"


def test_a_bad_class_filter_is_rejected_rather_than_silently_ignored() -> None:
    result = pt.search_properties({"query": "duration", "class": "NoSuchClass"})
    assert "error" in result
