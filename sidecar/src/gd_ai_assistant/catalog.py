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
from dataclasses import dataclass, replace
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
    #: Sprite frame the game draws for this id, when known (from the game itself).
    frame: str | None = None
    #: Grid footprint in GD units, when known.
    grid_size: float | None = None

    def to_json(self) -> dict[str, object]:
        """The wire shape of a search hit.

        `frame` and `grid_size` are sent whenever they are known, because they are
        the two fields that answer a question the name cannot: *which* spike is the
        thin one. Everything here comes from the game's own table, so none of it is
        a guess.
        """
        data: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "category": self.category,
        }
        if self.frame:
            data["frame"] = self.frame
        if self.grid_size is not None:
            data["grid_size"] = self.grid_size
        return data

    def haystack(self) -> str:
        """Lowercased text this entry matches against."""
        parts = [self.name, self.category, *self.aliases]
        if self.frame:
            parts.append(self.frame)
        return " ".join(parts).lower()


#: Category for an entry derived from a sprite frame, by the frame's leading word.
#:
#: Deliberately short and conservative: a frame that matches nothing becomes
#: "objects" rather than being filed under a guess. This is presentation, not
#: correctness -- the id and the frame are what the model acts on.
_CATEGORY_BY_PREFIX: dict[str, str] = {
    "spike": "hazards",
    "pit": "hazards",
    "saw": "hazards",
    "sawblade": "hazards",
    "thorn": "hazards",
    "block": "blocks",
    "pixel": "blocks",
    "slope": "blocks",
    "orb": "orbs",
    "pad": "pads",
    "portal": "portals",
    "coin": "collectibles",
    "ring": "boosters",
    "booster": "boosters",
    "grav": "boosters",
    "text": "text",
    "gradient": "decoration",
    "pulse": "triggers",
}


def _humanise(stem: str) -> str:
    """Turn a frame stem into readable words: `spike_02` -> `spike 2`.

    Leading zeros are stripped so that `block001` and `block01` do not appear as
    different words to a search, and the `_001` suffix GD appends to every frame is
    dropped because it carries no meaning.
    """
    words: list[str] = []
    for part in stem.split("_"):
        if not part or part in {"color", "001", "02", "hd", "uhd"}:
            continue
        words.append(str(int(part)) if part.isdigit() else part)
    return " ".join(words) or stem


def derive_entry(object_id: int, frame: str | None, grid_size: float | None) -> CatalogEntry:
    """Build an entry from what the game reports, inventing nothing."""
    name = f"object {object_id}"
    category = "objects"
    if frame:
        stem = frame[:-4] if frame.endswith(".png") else frame
        if stem.endswith("_001"):
            stem = stem[:-4]
        name = _humanise(stem)
        category = _CATEGORY_BY_PREFIX.get(stem.split("_")[0].rstrip("0123456789"), category)
    return CatalogEntry(
        id=object_id,
        name=name,
        category=category,
        source="game",
        frame=frame,
        grid_size=grid_size,
    )


class Catalog:
    """A searchable set of object type ids."""

    def __init__(
        self, entries: list[CatalogEntry], *, is_seed: bool, has_game_table: bool = False
    ) -> None:
        self._entries = entries
        self.is_seed = is_seed
        self.has_game_table = has_game_table
        #: How many times the game's table has been asked for, so a mod that cannot
        #: answer is not asked again on every turn of a long session.
        self.fetch_attempts = 0

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> list[CatalogEntry]:
        return list(self._entries)

    @property
    def needs_game_table(self) -> bool:
        """True until the game's own table has been absorbed."""
        return not self.has_game_table

    @property
    def fetch_allowed(self) -> bool:
        """False once asking for the table has failed too often.

        Bounded rather than unlimited: a mod that does not implement
        `list_object_catalog` (an older build, most likely) would otherwise cost a
        failed round trip at the start of every turn, forever.
        """
        return self.needs_game_table and self.fetch_attempts < 3

    def absorb_game_table(self, entries: list[dict]) -> int:
        """Merge the game's `id -> frame` table into this catalog.

        The game's table is the *base*: it is authoritative about which ids exist and
        what they look like. Curated seed and user entries override the presentation
        (name, category, aliases) of a specific id, which is how a readable name can
        sit on top of a real id without either source guessing about the other.

        Returns how many ids the table contributed, for the log line -- the number is
        the difference between "the mod answered" and "the mod answered nothing".
        """
        merged = {entry.id: entry for entry in self._entries}
        added = 0
        for raw in entries:
            try:
                object_id = int(raw["id"])
            except (KeyError, TypeError, ValueError):
                continue
            frame = raw.get("frame")
            grid_size = raw.get("grid_size")
            frame = str(frame) if isinstance(frame, str) else None
            grid_size = float(grid_size) if isinstance(grid_size, (int, float)) else None

            existing = merged.get(object_id)
            if existing is None:
                merged[object_id] = derive_entry(object_id, frame, grid_size)
                added += 1
                continue

            # A curated entry keeps its name and aliases and gains the game's facts.
            merged[object_id] = replace(existing, frame=frame, grid_size=grid_size)

        self._entries = sorted(merged.values(), key=lambda entry: entry.id)
        self.has_game_table = True
        self.is_seed = False
        return added

    def search(self, query: str, limit: int = 10) -> list[CatalogEntry]:
        """Rank entries against `query`.

        Token-AND rather than whole-string substring: a query like "thin spike" should
        still find the spikes, because no frame is literally called "thin". Entries
        matching more of the query's words rank first, and a word nothing matched is
        reported back by `search_object_catalog` instead of silently narrowing the
        result to nothing.
        """
        needle = query.strip().lower()
        if not needle:
            return []

        terms = [term for term in needle.split() if term]
        exact: list[tuple[int, CatalogEntry]] = []
        partial: list[tuple[int, CatalogEntry]] = []

        for entry in self._entries:
            haystack = entry.haystack()
            hits = sum(1 for term in terms if term in haystack)
            if hits == 0:
                continue
            name = entry.name.lower()
            is_exact = needle == name or needle in (alias.lower() for alias in entry.aliases)
            (exact if is_exact else partial).append((hits, entry))

        def rank(pair: tuple[int, CatalogEntry]) -> tuple[int, int]:
            # Most query words matched first, then lowest id: low ids are the plain
            # basics, which is a better default than an arbitrary ordering.
            hits, entry = pair
            return (-hits, entry.id)

        ordered = sorted(exact, key=rank) + sorted(partial, key=rank)
        return [entry for _, entry in ordered[: max(1, limit)]]


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

    The response says where the catalog came from and how big it is. Without that an
    empty result is ambiguous -- "no such object" and "the catalog is nearly empty"
    look identical to the model -- and the ambiguity is resolved by inventing an id,
    which is the exact failure this tool exists to prevent.

    It also reports which of the query's words matched nothing. A model asking for a
    "thin spike" is describing a *size*, and no object is named "thin"; telling it
    that "thin" matched nothing, while returning the spikes and their grid sizes,
    leaves it with everything it needs to choose.
    """
    query = str(arguments.get("query", ""))
    limit = int(arguments.get("limit") or 10)

    matches = catalog.search(query, limit)
    terms = [term for term in query.strip().lower().split() if term]
    matched_terms = {term for term in terms for entry in matches if term in entry.haystack()}
    unmatched = [term for term in terms if term not in matched_terms]

    if catalog.is_seed:
        note = (
            "This catalog is a seed and is nearly empty; an empty result does not "
            "mean the object does not exist. Tell the user you could not look it up "
            "rather than guessing an id."
        )
    elif unmatched:
        note = (
            f"No object is named {unmatched!r}. The results match the rest of the "
            "query; use `grid_size` (GD units, 30 = one block) and the frame name to "
            "choose between them."
        )
    else:
        note = None

    return {
        "query": query,
        "matches": [entry.to_json() for entry in matches],
        "total_in_catalog": catalog.size,
        "is_seed_catalog": catalog.is_seed,
        "source": "game" if catalog.has_game_table else "seed",
        "unmatched_terms": unmatched,
        "note": note,
    }
