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

} // namespace gdai::editor
