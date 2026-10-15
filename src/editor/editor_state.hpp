#pragma once

#include <matjson.hpp>

#include <string>
#include <utility>
#include <vector>

namespace gdai::editor {

/// A snapshot of editor state, for the `get_editor_state` request (SPEC.md 7.5).
///
/// Deliberately a value type holding no `GameObject*`: nothing may be carried
/// across a frame boundary or an await (SPEC.md 6.1.3 rule 5).
struct Snapshot {
    bool editorOpen = false;
    bool playtesting = false;
    int objectCount = 0;
    int editorLayer = 0;
    int selectedCount = 0;
    /// (object type id, count) pairs, most common first, capped at 20 entries.
    std::vector<std::pair<int, int>> selectedByType;
    std::string levelName;
    /// Persistent editor ID, or 0 when it could not be resolved.
    ///
    /// Not `GJGameLevel::m_levelID`, which is 0 for a level that has never been
    /// uploaded, and not the level name, which collides across levels and breaks
    /// on rename. Resolved on the main thread so no pointer escapes this struct
    /// (SPEC.md 6.1.3 rule 5).
    int levelId = 0;
};

/// Reads the current editor state.
///
/// **Must be called on the main thread** — it reads Cocos2d and GD state
/// (SPEC.md 6.1.3 rule 2).
Snapshot takeSnapshot();

/// Converts a snapshot to the protocol result object (SPEC.md 7.5).
///
/// Only fields the mod can supply confidently are emitted; the JSON Schema
/// marks the rest optional. Specifically:
///
/// - `ground_y` is omitted. Its derivation is not yet verified against the
///   game, and a wrong baseline would silently misplace generated objects.
/// - `camera` is omitted for the same reason.
/// - `z_layer` naming and `editor_layer` indexing remain unverified
///   (Appendix A #11), so `editor_layer` is reported raw, as the game stores it.
matjson::Value toJson(Snapshot const& snapshot);

/// TEMPORARY (spike): logs `getSaveString` for a few objects of distinct types,
/// so the key-to-property mapping can be derived from the game rather than from
/// community docs. Strictly read-only -- it calls no setters and mutates nothing.
///
/// Must be called on the main thread. Remove once the key map is captured.
void dumpSaveStringSamples();

/// TEMPORARY (spike): derives the save-string key map by differential probing.
///
/// For each property it creates a scratch object, dumps its save string, applies
/// exactly one setter or field change, dumps again, and reports which keys
/// changed. Doing this in-process is what makes verification cheap: the whole map
/// is confirmed in a single pass, with no manual editor actions and no
/// guesswork about which action touched which key.
///
/// All scratch objects are created with `noUndo` and removed afterwards, so the
/// level is left as it was found. Must be called on the main thread.
void runDifferentialProbe();

} // namespace gdai::editor
