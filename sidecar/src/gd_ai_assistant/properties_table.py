"""The save-string property table, and lookups over it (SPEC.md 7.5.3).

Where this comes from
---------------------
`data/properties.json` is generated from the Geode bindings by
`scripts/gen_properties.py`. The bindings annotate class members with the save-string
key they serialise under:

    // property 51
    int m_targetGroupID;

so "key 51 means the target group" is read out of the game's own structure rather than
guessed or probed one key at a time. Cross-checked against measurements taken in the
editor: 24/25 = z layer/order, 57 = groups, 51 = target group, 128/129 = scale,
155/156 = the colour indices. All agree.

What it is for
--------------
The mod can already read and write the eleven properties in its `kSpecs` table. Every
*other* property -- a move trigger's duration and easing, a pulse trigger's target
colour, a portal's target channel -- is reachable only by save-string key. Without a
key -> meaning table the model can only guess, and a guessed key silently edits the
wrong thing.

Two lookups matter, and they are different questions:

- **By name**, when the model knows what it wants ("duration", "easing") and needs to
  find out what to read or write. Class-scoped, because the binding annotations are
  per class and inheritance varies.
- **By key**, when something hands back a raw save string and the question is what a
  key *means*. Global, because the vocabulary is global and additive -- a key means the
  same thing on every object that has it.

The class hierarchy is resolved on the fly rather than expanded into the file: a
`RingObject` inherits `EffectGameObject`'s properties, and duplicating them into every
class would treble the file and invite the copies to drift.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("sidecar")

_TABLE_PATH = Path(__file__).resolve().parent / "data" / "properties.json"


@dataclass(frozen=True)
class PropertyEntry:
    """One save-string key, as one class sees it."""

    key: int
    name: str
    type: str
    #: The class the key was annotated on. Differs from the class asked about when the
    #: property is inherited, which is worth showing: it says where to look it up.
    defined_on: str

    def to_json(self) -> dict[str, object]:
        return {"key": self.key, "name": self.name, "type": self.type, "class": self.defined_on}


class PropertyTable:
    """The generated table, plus the lookups the tools need."""

    def __init__(self, raw: dict) -> None:
        self.gd_version: str = raw.get("gdVersion", "unknown")
        self.source: str = raw.get("source", "unknown")
        self._classes: dict[str, dict] = raw.get("classes", {})
        self._keys: dict[str, str] = raw.get("keyNames", {})
        self.conflicts: dict[str, list[str]] = raw.get("conflicts", {})
        self.supplement: dict = raw.get("supplement", {})

    # -- shape -----------------------------------------------------------

    @property
    def class_names(self) -> list[str]:
        return sorted(self._classes)

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def has_class(self, name: str) -> bool:
        return name in self._classes

    # -- class hierarchy --------------------------------------------------

    def _chain(self, class_name: str) -> list[str]:
        """The class and its ancestors, nearest first.

        Cycle-guarded: the bindings are generated, but a malformed file should produce a
        short chain rather than an infinite loop in the middle of a turn.
        """
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = class_name
        while current is not None and current not in seen:
            seen.add(current)
            if current in self._classes:
                chain.append(current)
            current = (self._classes.get(current) or {}).get("parent")
        return chain

    def properties_for_class(self, class_name: str) -> list[PropertyEntry]:
        """Every save-string property a class has, inherited ones included.

        Nearest definition wins: a subclass overriding a key shadows its parent, which is
        how the game's own serialiser behaves.
        """
        if not self.has_class(class_name):
            return []

        resolved: dict[int, PropertyEntry] = {}
        for owner in self._chain(class_name):
            for key, value in self._classes[owner].get("props", {}).items():
                member, kind = value[0], value[1]
                resolved.setdefault(int(key), PropertyEntry(int(key), member, kind, owner))
        return [resolved[key] for key in sorted(resolved)]

    # -- lookups ----------------------------------------------------------

    def name_for_key(self, key: int) -> str | None:
        """What a key means, independent of any class."""
        return self._keys.get(str(key))

    def search_by_name(
        self, query: str, class_name: str | None = None, limit: int = 25
    ) -> list[PropertyEntry]:
        """Find properties whose name contains `query`.

        Case-insensitive substring rather than exact match, because the members are C++
        field names: asking for "duration" should find `m_duration`, and asking for
        "ease" should find `m_easingType`.
        """
        needle = query.strip().lower()
        if not needle:
            return []

        # A leading `m_` is how the field is spelled, not how anyone asks for it.
        if needle.startswith("m_"):
            needle = needle[2:]

        entries = self.properties_for_class(class_name) if class_name else self.all_entries()
        return [entry for entry in entries if needle in entry.name.lower()][: max(1, limit)]

    def all_entries(self) -> list[PropertyEntry]:
        """Every property across every class, de-duplicated by key.

        Used when the caller does not know the class -- which is the normal case, since
        the mod does not report one. A key seen in several classes is reported once, from
        the class that defines it.
        """
        resolved: dict[int, PropertyEntry] = {}
        for name in self.class_names:
            for entry in self.properties_for_class(name):
                resolved.setdefault(entry.key, entry)
        return [resolved[key] for key in sorted(resolved)]


@lru_cache(maxsize=1)
def load_table() -> PropertyTable:
    """Load the generated table once per process."""
    try:
        raw = json.loads(_TABLE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Not fatal: the tool degrades to "I cannot answer that", which the prompt
        # already instructs the model to say rather than guess.
        log.warning(
            "property table missing at %s; run scripts/gen_properties.py to create it",
            _TABLE_PATH,
        )
        return PropertyTable({"gdVersion": "missing"})
    except json.JSONDecodeError as exc:
        log.warning("property table at %s is malformed: %s", _TABLE_PATH, exc)
        return PropertyTable({"gdVersion": "malformed"})
    return PropertyTable(raw)


def search_properties(arguments: dict) -> dict:
    """Handle the `search_properties` tool call.

    Two ways in, because the model asks two different questions:

    - `query` -- "what property controls duration?" (a name fragment)
    - `key`   -- "what does key 51 mean?" (a save-string key from a dump)

    `class_name` narrows a name search. It is optional because the mod does not report
    object classes, so a model will usually not have one; without it the search spans
    every class, which gives a superset and says so.
    """
    table = load_table()
    query = str(arguments.get("query") or "").strip()
    raw_key = arguments.get("key")
    class_name = str(arguments.get("class") or "").strip() or None

    if class_name and not table.has_class(class_name):
        return {
            "error": f"unknown class {class_name!r}",
            "hint": "omit `class` to search every class",
            "classes_available": table.class_names[:12],
        }

    if raw_key is not None:
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            return {"error": f"key must be an integer, got {raw_key!r}"}
        name = table.name_for_key(key)
        return {
            "key": key,
            "name": name,
            "known": name is not None,
            "note": None
            if name is not None
            else (
                "This key is not annotated in the game's bindings. Do not guess what it "
                "means; read the object instead and tell the user what you could not "
                "identify."
            ),
            "gd_version": table.gd_version,
        }

    if not query:
        return {"error": "provide either `query` (a name) or `key` (an integer)"}

    matches = table.search_by_name(query, class_name)
    return {
        "query": query,
        "class": class_name,
        "searched_all_classes": class_name is None,
        "matches": [entry.to_json() for entry in matches],
        "table": {
            "gd_version": table.gd_version,
            "classes": len(table.class_names),
            "keys": table.key_count,
        },
        "note": None
        if matches
        else (
            "No property name contains that text. Properties are C++ field names such as "
            "`m_duration` or `m_easingType`; try a shorter fragment, or use `key` if you "
            "have a save-string key."
        ),
    }


def describe_class(arguments: dict) -> dict:
    """Handle the `describe_class` tool call: every property a class has."""
    table = load_table()
    class_name = str(arguments.get("class") or "").strip()
    if not class_name:
        return {"error": "`class` is required", "classes_available": table.class_names}

    # Accept `GameObject` for `m_GameObject`-style spellings and a leading `m_`.
    if class_name.startswith("m_"):
        class_name = class_name[2:]

    if not table.has_class(class_name):
        return {
            "error": f"unknown class {class_name!r}",
            "classes_available": table.class_names,
        }

    entries = table.properties_for_class(class_name)
    return {
        "class": class_name,
        "parent": (table._classes.get(class_name) or {}).get("parent"),
        "property_count": len(entries),
        "properties": [entry.to_json() for entry in entries],
        "note": (
            "These are save-string keys. The mod reads and writes a fixed set of named "
            "properties directly; a key outside that set can be read from an object's "
            "save string but not written by name."
        ),
        "gd_version": table.gd_version,
    }
