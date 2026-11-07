#include "level_revision.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>

using namespace geode::prelude;

namespace gdai::editor {
namespace {

unsigned int countOf(CCArray* array) {
    return array == nullptr ? 0u : array->count();
}

void const* topOf(CCArray* array) {
    return array == nullptr || array->count() == 0 ? nullptr : array->lastObject();
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
        bump();
        return;
    }
    if (now != m_last) {
        m_last = now;
        bump();
    }
}

void LevelRevision::forgetEditor() {
    m_haveLast = false;
}

} // namespace gdai::editor
