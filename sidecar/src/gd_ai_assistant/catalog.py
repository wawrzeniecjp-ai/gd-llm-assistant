"""Object-id catalog and the local `search_object_catalog` tool (SPEC.md 6.2.3).

The catalog exists to stop the model inventing object ids. That makes its
*incompleteness* a correctness problem rather than a cosmetic one: a short catalog
that answers confidently is worse than one that says "I do not know", because the
model will fill the gap from memory and the result is a wrong object silently
placed in the user's level.

So the seed below contains only ids that can be justified, every response says how
many entries the catalog holds and whether it is still a seed, and the prompt
instructs the model to tell the user when a lookup comes up empty rather than
guessing. Populating it properly is a Phase 5 task (see SPEC.md 15): it needs a
verified source, not a plausible-looking list.

A user-supplied catalog file merges over the seed, so ids can be added without
editing this module. See `SidecarConfig.catalog_path`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("sidecar")

#: Seed entries, as `(id, name, category, aliases)`.
#:
#: Deliberately tiny. These are the ids that come up constantly and are stable
#: across versions; anything less certain is left out on purpose. Adding a
#: plausible-but-unverified id here is how the catalog would start lying.
_SEED: tuple[tuple[int, str, str, tuple[str, ...]], ...] = (
    (1, "Block", "blocks", ("square", "cube", "basic block")),
    (8, "Spike", "hazards", ("spike", "hazard", "pointy")),
)


@dataclass(frozen=True)
class CatalogEntry:
    id: int
    name: str
    category: str
    aliases: tuple[str, ...] = ()
    source: str = "seed"

    def to_json(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "category": self.category}

    def haystack(self) -> str:
        """Lowercased text this entry matches against."""
        return " ".join((self.name, self.category, *self.aliases)).lower()


class Catalog:
    """A searchable set of object type ids."""

    def __init__(self, entries: list[CatalogEntry], *, is_seed: bool) -> None:
        self._entries = entries
        self.is_seed = is_seed

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> list[CatalogEntry]:
        return list(self._entries)

    def search(self, query: str, limit: int = 10) -> list[CatalogEntry]:
        """Rank entries against `query`.

        Exact name and alias matches come first, then substring matches, so
        searching "spike" returns the spike rather than the first thing that
        happens to contain those letters.
        """
        needle = query.strip().lower()
        if not needle:
            return []

        exact: list[CatalogEntry] = []
        partial: list[CatalogEntry] = []
        for entry in self._entries:
            name = entry.name.lower()
            if needle == name or needle in (alias.lower() for alias in entry.aliases):
                exact.append(entry)
            elif needle in entry.haystack():
                partial.append(entry)

        return (exact + partial)[: max(1, limit)]


def _load_user_entries(path: Path) -> list[CatalogEntry]:
    """Read a user catalog file, tolerating a malformed one.

    Tolerated rather than fatal: bad catalog data should not stop the sidecar
    starting, and a warning naming the file is enough for the user to fix it. The
    alternative is a sidecar that refuses to run because of an optional extra.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("ignoring catalog file %s: %s", path, exc)
        return []

    if not isinstance(raw, list):
        log.warning("ignoring catalog file %s: expected a JSON list", path)
        return []

    entries: list[CatalogEntry] = []
    for item in raw:
        if not isinstance(item, dict) or "id" not in item or "name" not in item:
            log.warning("ignoring catalog entry without id/name in %s", path)
            continue
        try:
            entry_id = int(item["id"])
        except (TypeError, ValueError):
            log.warning("ignoring catalog entry with non-integer id in %s", path)
            continue
        aliases = item.get("aliases") or []
        entries.append(
            CatalogEntry(
                id=entry_id,
                name=str(item["name"]),
                category=str(item.get("category", "uncategorised")),
                aliases=tuple(str(alias) for alias in aliases),
                source=str(path),
            )
        )
    return entries


@lru_cache(maxsize=4)
def load_catalog(catalog_path: str | None = None) -> Catalog:
    """Build the catalog once, merging any user file over the seed."""
    entries = [
        CatalogEntry(id=entry_id, name=name, category=category, aliases=aliases)
        for entry_id, name, category, aliases in _SEED
    ]
    by_id = {entry.id: entry for entry in entries}

    if catalog_path:
        path = Path(catalog_path)
        if path.is_file():
            for entry in _load_user_entries(path):
                # User entries win: they are the correction mechanism for a wrong
                # or outdated seed entry.
                by_id[entry.id] = entry
            log.info("catalog: %d entries after merging %s", len(by_id), path)
        else:
            log.warning("catalog_path %s does not exist; using the seed only", path)

    return Catalog(list(by_id.values()), is_seed=len(by_id) == len(_SEED))


def search_object_catalog(catalog: Catalog, arguments: dict) -> dict:
    """Handle the `search_object_catalog` tool call.

    The response deliberately carries `total_in_catalog` and `is_seed_catalog`.
    Without them an empty result is ambiguous -- "no such object" and "the catalog
    is nearly empty" look identical to the model -- and the ambiguity is resolved
    by inventing an id, which is the exact failure this tool exists to prevent.
    """
    query = str(arguments.get("query", ""))
    limit = int(arguments.get("limit") or 10)

    matches = catalog.search(query, limit)
    return {
        "query": query,
        "matches": [entry.to_json() for entry in matches],
        "total_in_catalog": catalog.size,
        "is_seed_catalog": catalog.is_seed,
        "note": (
            "This catalog is a seed and is nearly empty; an empty result does not "
            "mean the object does not exist. Tell the user you could not look it up "
            "rather than guessing an id."
            if catalog.is_seed and not matches
            else None
        ),
    }
