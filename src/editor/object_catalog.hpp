#pragma once

#include <matjson.hpp>

#include <string>
#include <vector>

namespace gdai::editor {

/// One entry of the game's own object table (SPEC.md 7.5, `list_object_catalog`).
///
/// This is **not** a hand-written catalog. It is read out of `ObjectToolbox`, which
/// is the table the game itself uses to map an object id to the sprite it draws.
/// The distinction is the whole point of the request: a guessed id places the wrong
/// object silently, and there is no way to guess a few thousand of them correctly
/// (SPEC.md 15).
struct CatalogEntry {
    /// Object type id, as used by `create_objects`.
    int id = 0;
    /// Sprite frame the game draws for this id, e.g. `spike_02_001.png`.
    ///
    /// Empty when the toolbox has no frame for the id, which happens for types that
    /// are drawn programmatically (triggers, most notably).
    std::string frame;
    /// Grid footprint in GD units, from `ObjectToolbox::gridNodeSizeForKey`.
    ///
    /// 0 when the game reports nothing. Reported because it is the only *size*
    /// signal available without instantiating each object, and size is what
    /// separates "spike" from "thin spike".
    float gridSize = 0.f;
};

/// One page of the catalog.
struct CatalogPage {
    std::vector<CatalogEntry> entries;
    /// Total number of ids the game knows about, regardless of paging.
    int total = 0;
    /// Offset to request next, or -1 when this is the last page.
    int nextOffset = -1;
};

/// True when the game's object table can be read.
///
/// False before the game's resources are loaded. Callers should treat that as
/// "try again later" rather than as an error, exactly like a missing `EditorUI`.
bool catalogAvailable();

/// Reads one page of the catalog, ordered by id.
///
/// Paged because the table is large enough that one frame would be wasteful and,
/// near the protocol's 1 MB limit, fragile. **Must be called on the main thread**: it
/// reads the game's toolbox, which is Cocos-adjacent state (SPEC.md 6.1.3).
CatalogPage takeCatalogPage(int offset, int limit);

/// Converts a page to the protocol result object (SPEC.md 7.5).
matjson::Value catalogPageToJson(CatalogPage const& page);

} // namespace gdai::editor
