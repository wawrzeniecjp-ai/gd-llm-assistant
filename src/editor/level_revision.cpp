#include "level_revision.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/EditorUI.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>

#include <functional>
#include <string_view>

using namespace geode::prelude;

namespace gdai::editor {
namespace {

/// Selected objects hashed per check; see the class comment.
constexpr unsigned int kSelectionHashCap = 64;

/// Frames between selection checks (~30 ms at 240 fps, ~130 ms at 60).
constexpr unsigned int kSelectionHashEvery = 8;

unsigned int countOf(CCArray* array) {
    return array == nullptr ? 0u : array->count();
}

void const* topOf(CCArray* array) {
    return array == nullptr || array->count() == 0 ? nullptr : array->lastObject();
}

void mix(std::size_t& seed, std::size_t value) {
    seed ^= value + 0x9e3779b97f4a7c15ull + (seed << 6) + (seed >> 2);
}

} // namespace

LevelRevision& LevelRevision::get() {
    static LevelRevision instance;
    return instance;
}

void LevelRevision::bump() {
    m_revision.fetch_add(1, std::memory_order_acq_rel);
}

void LevelRevision::poll(LevelEditorLayer* editor) {
    if (editor == nullptr) {
        return;
    }

    Fingerprint now;
    now.objectCount = countOf(editor->getAllObjects());
    now.undoCount = countOf(editor->m_undoObjects);
    now.redoCount = countOf(editor->m_redoObjects);
    now.undoTop = topOf(editor->m_undoObjects);
    now.redoTop = topOf(editor->m_redoObjects);

    if (!m_haveLast) {
        // The first look at an editor is a new level as far as any reader is
        // concerned, so it counts as a change too.
        m_last = now;
        m_haveLast = true;
        m_selection = {};
        m_frame = 0;
        bump();
        return;
    }
    // Why the revision moved, logged at debug level: S-L10 found a deselect/reselect
    // bumping it three times, and this says which signal did.
    std::string_view reason;
    if (now.objectCount != m_last.objectCount) reason = "object count";
    else if (now.undoCount != m_last.undoCount || now.undoTop != m_last.undoTop) reason = "undo list";
    else if (now.redoCount != m_last.redoCount || now.redoTop != m_last.redoTop) reason = "redo list";
    m_last = now;

    if (++m_frame % kSelectionHashEvery == 0) {
        SelectionPrint selection;
        if (auto* ui = editor->m_editorUI) {
            unsigned int hashed = 0;
            auto hashOne = [&](GameObject* object) {
                if (object == nullptr || hashed >= kSelectionHashCap) {
                    return;
                }
                ++hashed;
                mix(selection.members, std::hash<void const*>{}(object));
                auto const save = object->getSaveString(editor);
                mix(selection.content,
                    std::hash<std::string_view>{}(std::string_view(save.data(), save.size())));
            };
            hashOne(ui->m_selectedObject);
            if (auto* selected = ui->m_selectedObjects) {
                for (auto* object : CCArrayExt<GameObject*>(selected)) {
                    hashOne(object);
                }
            }
        }
        // Same objects, different content: an in-place edit (Edit Object and the
        // like). A different set is a selection change, which only re-baselines.
        if (selection.members == m_selection.members
            && selection.content != m_selection.content && reason.empty()) {
            reason = "selection content";
        }
        m_selection = selection;
    }

    if (!reason.empty()) {
        bump();
        log::debug("revision {}: {}", current(), reason);
    }
}

void LevelRevision::forgetEditor() {
    m_haveLast = false;
}

} // namespace gdai::editor
