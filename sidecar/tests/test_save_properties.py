"""Save-string property writes (SPEC.md 7.5.4).

The mod-side rewrite is exercised in the editor, not here. These tests cover the half
that lives in the sidecar and can therefore be wrong quietly:

- **Name resolution.** The model asks for "no touch"; the game calls the member
  `m_isNoTouch`. Getting this wrong means every such request fails, and it fails with
  "no property matches" -- which reads like the feature is missing rather than the
  lookup being wrong.
- **Value encoding.** Booleans are `1`/`0` in a save string, and `true` reads back as
  zero: a wrong value rather than an error, which is the worst failure mode available.
- **Refusals.** Keys 1-3 must be rejected here rather than in the mod, so the model is
  told to use `modify_objects` instead of receiving a generic error after a round trip.
"""

from __future__ import annotations

from gd_ai_assistant import save_properties as sp


def test_no_touch_resolves_to_the_flag_the_game_actually_has() -> None:
    """The case that motivated the feature, in every spelling a model might use.

    The member is `m_isNoTouch`, so the schema's normalisation has to bridge "no touch"
    and "nontouch" to "notouch" before matching.
    """
    for query in ["no touch", "no-touch", "nontouch", "NoTouch", "no_touch"]:
        key, info = sp.resolve_key(query)
        assert key == 121, f"{query!r} resolved to {key!r}"
        assert info["matched_name"] == "isNoTouch"


def test_a_number_is_taken_as_a_key() -> None:
    assert sp.resolve_key(51)[0] == 51
    assert sp.resolve_key("51")[0] == 51


def test_a_query_that_matches_nothing_says_how_to_recover() -> None:
    key, info = sp.resolve_key("zzzz-not-a-real-property")
    assert key is None
    assert "error" in info
    assert "search_properties" in info["hint"]


def test_ambiguity_is_surfaced_rather_than_hidden() -> None:
    """"easing" exists on several classes, and the mod does not report a class.

    Picking one silently would be a guess. The alternatives are what lets the model ask
    or choose deliberately.
    """
    key, info = sp.resolve_key("easing")
    assert key is not None
    assert info["other_matches"], "ambiguous names must offer alternatives"


def test_the_prefix_match_beats_a_longer_name_that_merely_contains_it() -> None:
    """`easingType` (key 30) is the move-trigger field; `followEasing` (213) is the
    camera trigger's. Ranking by "name adds least to the query" keeps them apart."""
    keys = [entry.key for entry in sp.find_property("easing", limit=6)]
    assert 30 in keys
    assert keys.index(30) < keys.index(213) if 213 in keys else True


def test_booleans_encode_as_one_and_zero() -> None:
    """`true` in a save string is read back as zero, so this must not pass it through."""
    assert sp.encode_value(True, "bool") == "1"
    assert sp.encode_value(False, "bool") == "0"


def test_whole_floats_are_written_without_a_decimal_point() -> None:
    """Matches what the game writes itself, so a save string stays stable across our
    writes and the game's."""
    assert sp.encode_value(1.0, "float") == "1"
    assert sp.encode_value(2.5, "float") == "2.5"
    assert sp.encode_value(7, "int") == "7"


def test_encoded_properties_pass_their_text_through_untouched() -> None:
    """Group lists and HSV have their own encodings; re-encoding them here would be
    re-deriving a format the game already defines."""
    assert sp.encode_value("1.2.3", "raw") == "1.2.3"
    assert sp.encode_value("255a1a1a1a1", "raw") == "255a1a1a1a1"


def test_reserved_keys_name_the_alternative_instead_of_just_refusing() -> None:
    """A refusal that does not say what to do instead wastes a round trip."""
    for key in (1, 2, 3):
        reason = sp.check_writable(key)
        assert reason is not None
        assert "modify_objects" in reason or "create" in reason


def test_ordinary_keys_are_writable() -> None:
    assert sp.check_writable(121) is None
    assert sp.check_writable(51) is None


def test_reading_a_result_names_every_known_key_and_flags_the_rest() -> None:
    """The mod reports `key,value` and knows nothing about meaning. A key the table does
    not know is reported as unnamed rather than dropped: "this object has a property I
    cannot name" is useful, and omission would make the object look simpler than it is.
    """
    result = {
        "object_id": 4821,
        "properties": [
            {"key": 121, "value": "1"},
            {"key": 51, "value": "7"},
            {"key": 999999, "value": "x"},
        ],
        "count": 3,
    }
    described = sp.describe_save_properties(result)

    names = {entry["key"]: entry["name"] for entry in described["properties"]}
    assert names[121] == "isNoTouch"
    assert names[51] == "targetGroupID"
    assert names[999999] is None
    assert described["unnamed_keys"] == [999999]
    assert "Do not guess" in described["note"]
