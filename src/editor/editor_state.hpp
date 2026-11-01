#pragma once

#include <matjson.hpp>

#include <string>
#include <utility>
#include <vector>

#include "camera.hpp"

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
    /// Where the view is looking, including the rectangle of the level on screen.
    ///
    /// Read here rather than on demand because this is already the one main-thread hop a
    /// caller makes before deciding what to do, and "which objects can I actually see" is
    /// part of deciding that. `readCamera` may refuse, so it is paired with a flag.
    Camera camera;
    bool hasCamera = false;
};

/// Reads the current editor state.
///
/// **Must be called on the main thread** — it reads Cocos2d and GD state
/// (SPEC.md 6.1.3 rule 2).
Snapshot takeSnapshot();

/// Converts a snapshot to the protocol result object (SPEC.md 7.5).
///
/// `ground_y` is omitted: it cannot be read reliably from the APIs that look like they
/// provide it. The ground layer's position tracks the camera rather than describing the
/// level (see `cocosYToLevel`'s notes in the implementation), so reporting it would be
/// worse than reporting nothing. Ground placement is instead guided by `lowest_y` from
/// `list_object_types`, which is a real observed object position.
///
/// **`camera` is now reported**, which it was not: an earlier version omitted it for the
/// same reason as `ground_y`, because the only obvious source -- the ground layer -- tracked
/// the camera instead of describing the level. It is read from the object layer's transform
/// instead (SPEC.md 7.5.7), which is what actually maps level space to the screen, so the
/// position, the zoom and the visible rectangle are all answers rather than guesses.
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

/// TEMPORARY (dev): exercises the transaction path end to end.
///
/// Opens a transaction, creates objects with `noUndo`, records them, ends the
/// turn, then reverts and checks the object count returns to where it started.
/// It is the only way to test `TransactionManager` without the Phase 2 request
/// handlers, and deliberately restricts itself to APIs already proven safe in
/// earlier probes -- it never calls `handleAction`/`undoLastAction`, which is
/// what crashed the game once.
void runTransactionProbe();

/// TEMPORARY (dev): determines the **shape** of an undo entry.
///
/// The first half measured that the default path gives one undo entry per
/// created object, so a batch create would need N Ctrl+Z presses.
///
/// **This probe exists because an earlier attempt at grouping crashed the game.**
/// An `UndoObject` was built with `createWithArray(gameObjects, UndoCommand::New)`
/// and handed to `undoLastAction()`, which reached
/// `LevelEditorLayer::handleAction` and dereferenced a null at +0x38. So this half
/// constructs nothing: it reads what the *game* puts on the stack, and never calls
/// `handleAction`/`undoLastAction`.
///
/// Result (GD 2.2081): `m_undoObjects` holds plain `UndoObject*` -- **not**
/// `CCArray` wrappers -- each with `m_command == UndoCommand::New` and
/// `m_objects == nullptr`. A single-object entry stores its object in
/// `m_objectCopy`, not `m_objects`, and `UndoCommand` has no multi-object *create*
/// command (see SPEC.md 6.1.5 for what that implies).
///
/// **Read-only with respect to the undo stack**: it never calls
/// `handleAction`/`undoLastAction`, and clears both stacks before returning.
void runUndoShapeProbe();

/// TEMPORARY (spike S3, first half): measures how the *undo* stack behaves for
/// created objects, and whether a transaction can register its own undo entries.
///
/// Answers the question SPEC.md 6.1.5 leaves open: whether one Ctrl+Z can revert
/// a whole assistant turn (criterion A2), or whether the bookkeeping fallback is
/// required. The two halves are independent:
///
///   1. `noUndo = false` -- does creating N objects produce N undo entries (one
///      Ctrl+Z removes one object) or one entry (removes all N)?
///   2. `addToUndoList` -- can a transaction register its own grouped entry?
///
/// Scratch objects are removed afterwards. **This one is not purely read-only**:
/// it creates objects that enter the undo stack, so it deliberately clears that
/// stack first, and the level is left as found only if the last step succeeds.
/// Must be called on the main thread.
void runUndoProbe();

} // namespace gdai::editor
