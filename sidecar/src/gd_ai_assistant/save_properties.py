"""Save-string property tools (SPEC.md 7.5.4).

The mod can only *write* a fixed set of eleven named properties. Everything else an
object carries -- a trigger's "no touch" flag, a move trigger's easing, a pulse
trigger's target colour -- is reachable only through the object's save string, keyed by
number.

`set_save_property` closes that gap by rewriting the save string and letting the game
rebuild the object from it. That is not a shortcut; it is the only interface that
reaches those fields, and the game can only turn a save string into an object by
creating one. The consequence is worth stating plainly to the model: **the object is
replaced and its unique id changes.**

Two things have to happen on this side rather than in the mod:

- **Name resolution.** The model should be able to say `no touch` and have the right key
  found. The mod has no name table; the sidecar's generated one does (SPEC.md 7.5.3).
- **Value encoding.** Save strings have their own text encodings -- booleans are `0`/`1`,
  group lists are dot-separated, HSV is `h.a.s.a.v...`. Coercing in the mod would mean
  re-deriving that format there; here it is one function with the table's type
  information to guide it.
"""

from __future__ import annotations

from . import properties_table

#: Keys the mod refuses to write through the save string, with the reason.
#:
#: Position especially: the save string stores level Y while every other interface uses
#: Cocos Y, so a write here would move the object silently by 90 units.
_RESERVED_KEYS: dict[int, str] = {
    1: "the object's type id -- create a different object instead",
    2: "x position -- use modify_objects, which handles the coordinate conversion",
    3: "y position -- use modify_objects, which handles the coordinate conversion",
}

#: Spelling and synonym aliases only. Phrasing is handled by normalisation, so
#: "no touch" needs no entry here.
_ALIASES: dict[str, str] = {
    "colour": "color",
    "colours": "color",
    "grayscale": "greyscale",
    "hue": "hsv",
    "visibility": "visible",
    "invisible": "visible",
}


def _normalise(text: str) -> str:
    """Lowercase and strip everything that is not a letter or digit.

    This is what makes plain English reachable. The bindings name the property
    `m_isNoTouch`, so "no touch" and "no-touch" have to find it -- and with the
    separators gone, `notouch` is a suffix of `isnotouch`. Keeping the separators would
    make every query depend on guessing the camelCase exactly.
    """
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _rank(name: str, needle: str) -> int | None:
    """How well a member name matches, or None when it does not.

    Ranked rather than filtered, because a first-match-wins rule picks badly: "easing"
    matches both `easingType` and `followEasing`, and returning whichever came first
    would hand a move trigger the camera trigger's field.

    Prefix and suffix score **the same**, deliberately. Both are real patterns -- the
    game spells flags `isNoTouch` (suffix) and qualifiers `easingType` (prefix) -- so
    neither is intrinsically a better match, and preferring one silently breaks the
    other. The tie-break decides instead: the name that adds least to the query wins.
    """
    if not needle:
        return None
    if name == needle:
        return 100
    if name.endswith(needle) or name.startswith(needle):
        return 75
    if needle in name:
        return 50
    return None


def _variants(text: str) -> list[str]:
    """Normalised forms of a query worth trying.

    "no touch", "no-touch" and "nontouch" are the same words, and normalisation alone
    only unifies the first two -- `nontouch` keeps its "non", while the member is
    `isNoTouch`. So a leading "non" is also tried as "no". Narrow on purpose: exactly one
    spelling variant, for the one English prefix pair that actually differs.
    """
    needle = _normalise(_ALIASES.get(text.strip().lower(), text))
    if not needle:
        return []
    variants = [needle]
    if needle.startswith("non") and len(needle) > 3:
        variants.append("no" + needle[3:])
    return variants


def find_property(text: str, limit: int = 8) -> list:
    """Member-name matches for `text`, best first.

    Sorted by score, then by how many characters the name adds to the query -- so
    `easingType` beats `followEasing` for "easing" -- then by key, so the order is stable
    across runs.
    """
    table = properties_table.load_table()
    needles = _variants(text)
    if not needles:
        return []

    best_score: dict[int, int] = {}
    by_key = {}
    for entry in table.all_entries():
        name = _normalise(entry.name)
        for needle in needles:
            score = _rank(name, needle)
            if score is None:
                continue
            if score > best_score.get(entry.key, -1):
                best_score[entry.key] = score
                by_key[entry.key] = entry

    stripped = len(_normalise(text))
    scored = [
        (best_score[entry.key], len(entry.name) - stripped, entry.key, entry)
        for entry in by_key.values()
    ]
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [entry for _score, _extra, _key, entry in scored[:limit]]


def encode_value(value: object, kind: str | None) -> str:
    """Turn a tool argument into the exact text a save string should carry.

    `kind` comes from the property table (`bool`, `int`, `float`, `string`, `raw`) and
    decides what a bare boolean or number becomes. Without it everything falls back to
    the obvious rendering, which is right for numbers and wrong for booleans -- and a
    boolean written as `true` reads back as zero, which is a wrong value rather than an
    error.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        # Whole floats are written without a decimal point: the game parses both, but
        # `1` is what it writes itself, so matching it keeps save strings stable across
        # our writes and the game's.
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return str(value)


def resolve_key(name_or_key: object) -> tuple[int | None, dict]:
    """Find the save-string key a caller means.

    Accepts a number, or a name. Name resolution goes through the generated table, so it
    finds whatever the bindings actually call the field rather than what seems likely --
    "no touch" is `m_isNoTouch`, which is not guessable from the words.
    """
    if isinstance(name_or_key, int) and not isinstance(name_or_key, bool):
        return name_or_key, {"resolved_by": "key"}

    text = str(name_or_key or "").strip()
    if not text:
        return None, {"error": "provide `key` (a number) or `property` (a name)"}

    # A bare number sent as a string is still a key.
    if text.lstrip("-").isdigit():
        return int(text), {"resolved_by": "key"}

    matches = find_property(text)
    if not matches:
        return None, {
            "error": f"no property name matches {text!r}",
            "hint": (
                "Use search_properties to find the right name or key first, then pass "
                "the key number."
            ),
        }

    # `find_property` already ranks, so the first match is the best one.
    chosen = matches[0]
    return chosen.key, {
        "resolved_by": "name",
        "matched_name": chosen.name,
        "matched_class": chosen.defined_on,
        "matched_type": chosen.type,
        # Alternatives matter: a name can exist on several classes, and the mod does not
        # report which class an object is, so this is how the caller can choose.
        "other_matches": [entry.to_json() for entry in matches[1:6]],
    }


def describe_save_properties(result: dict) -> dict:
    """Add names and types to a mod `get_save_properties` result.

    The mod hands over `key,value` pairs and knows nothing about what they mean. The
    table turns them into something readable, and a key the table does not know is
    reported as unnamed rather than omitted -- "this object has a property I cannot name"
    is useful information, and dropping it would make the object look simpler than it is.
    """
    table = properties_table.load_table()
    described: list[dict] = []
    unknown: list[int] = []

    for entry in result.get("properties", []):
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, int):
            continue
        name = table.name_for_key(key)
        item: dict = {"key": key, "value": entry.get("value"), "name": name}
        if name is None:
            unknown.append(key)
        described.append(item)

    return {
        "object_id": result.get("object_id"),
        "properties": described,
        "count": len(described),
        "unnamed_keys": unknown,
        "note": (
            "A key with `name: null` is not annotated in the game's bindings. Do not "
            "guess what it does."
            if unknown
            else None
        ),
        "table_gd_version": table.gd_version,
    }


def check_writable(key: int) -> str | None:
    """Why this key cannot be written, or None when it can."""
    return _RESERVED_KEYS.get(key)
