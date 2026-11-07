#pragma once

#include <atomic>
#include <cstdint>

class LevelEditorLayer;

namespace gdai::editor {

/// A counter that moves whenever the level's objects change (LEVEL-DESIGN-SPEC.md 4.3.2).
///
/// The sidecar reads the level in pages (`get_level_objects`), and a page boundary is
/// a place where the user can edit. Comparing the revision across pages is how it
/// tells a consistent snapshot from one stitched together around an edit.
///
/// **How changes are noticed.** The ideal would be hooks on every mutation path, but
/// the editor's undo/redo entry points are inlined on Windows (`addToUndoList`,
/// `LevelEditorLayer::undoLastAction`, `redoLastAction` are `win inline` in the
/// bindings), so they cannot be hooked. Instead, once per frame, `poll` compares a
/// cheap fingerprint of the editor:
///
/// - the object count (creation and deletion, by any route);
/// - the sizes of the undo and redo lists, and the identity of the newest entry on
///   each (every undoable user edit -- including a move or a property change --
///   pushes an entry; an undo or redo moves one between the lists; the identity
///   check covers a full list where a push drops the oldest entry and the size stays
///   the same).
///
/// The mod's own mutations are mostly made with `noUndo` and so are invisible to
/// the fingerprint; the job queue calls `bump` for those instead.
///
/// Over-counting is harmless (a reader just restarts); under-counting is the failure
/// to avoid. Spike S-L10 checks every mutation path against this.
class LevelRevision {
public:
    static LevelRevision& get();

    /// The current revision. Safe from any thread.
    [[nodiscard]] std::int64_t current() const {
        return m_revision.load(std::memory_order_acquire);
    }

    /// Records a change made by the mod itself. Main thread.
    void bump();

    /// Compares the editor's fingerprint with the last one and bumps on a difference.
    /// Main thread, once per frame.
    void poll(LevelEditorLayer* editor);

    /// Forgets the fingerprint, for when the editor closes. The counter itself is
    /// never reset: a revision seen before a close must not be seen again after.
    void forgetEditor();

private:
    LevelRevision() = default;

    struct Fingerprint {
        unsigned int objectCount = 0;
        unsigned int undoCount = 0;
        unsigned int redoCount = 0;
        void const* undoTop = nullptr;
        void const* redoTop = nullptr;

        bool operator==(Fingerprint const&) const = default;
    };

    std::atomic<std::int64_t> m_revision{ 0 };
    Fingerprint m_last;
    bool m_haveLast = false;
};

} // namespace gdai::editor
