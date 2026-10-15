#include "editor_state.hpp"

#include <Geode/binding/EditorUI.hpp>
#include <Geode/binding/GJGameLevel.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/binding/PlayLayer.hpp>

#include <algorithm>

namespace gdai::editor {
namespace {

/// GD units per grid block (SPEC.md 7.5). A game constant, not a per-level one.
constexpr int kGridSize = 30;

/// `by_type` is capped at 20 entries (SPEC.md 7.5), so an enormous selection
/// cannot produce an oversized response frame.
constexpr std::size_t kMaxTypeEntries = 20;

void countByType(Snapshot& snapshot, cocos2d::CCArray* selected) {
    std::vector<std::pair<int, int>> counts;

    auto const total = selected->count();
    for (unsigned int i = 0; i < total; ++i) {
        auto* object = static_cast<GameObject*>(selected->objectAtIndex(i));
        if (object == nullptr) {
            continue;
        }
        auto const objectType = object->m_objectID;
        auto const found = std::find_if(
            counts.begin(), counts.end(),
            [objectType](auto const& entry) { return entry.first == objectType; }
        );
        if (found != counts.end()) {
            ++found->second;
        } else {
            counts.emplace_back(objectType, 1);
        }
    }

    // Most common first, so the cap keeps the informative entries.
    std::sort(counts.begin(), counts.end(), [](auto const& lhs, auto const& rhs) {
        return lhs.second > rhs.second;
    });
    if (counts.size() > kMaxTypeEntries) {
        counts.resize(kMaxTypeEntries);
    }
    snapshot.selectedByType = std::move(counts);
}

} // namespace

Snapshot takeSnapshot() {
    Snapshot snapshot;

    auto* editor = LevelEditorLayer::get();
    snapshot.editorOpen = editor != nullptr;
    if (editor == nullptr) {
        return snapshot;
    }

    // A PlayLayer alongside an open editor means playtesting.
    snapshot.playtesting = PlayLayer::get() != nullptr;
    snapshot.objectCount = static_cast<int>(editor->m_objectVector.size());
    snapshot.editorLayer = static_cast<int>(editor->m_currentLayer);

    if (editor->m_level != nullptr) {
        snapshot.levelName = editor->m_level->m_levelName;
    }

    if (editor->m_editorUI != nullptr) {
        if (auto* selected = editor->m_editorUI->getSelectedObjects()) {
            snapshot.selectedCount = static_cast<int>(selected->count());
            countByType(snapshot, selected);
        }
    }

    return snapshot;
}

matjson::Value toJson(Snapshot const& snapshot) {
    auto result = matjson::Value::object();

    // The three fields the schema requires.
    result["object_count"] = snapshot.objectCount;
    result["grid_size"] = kGridSize;
    result["playtesting"] = snapshot.playtesting;

    result["editor_layer"] = snapshot.editorLayer;

    if (!snapshot.levelName.empty()) {
        auto level = matjson::Value::object();
        level["name"] = snapshot.levelName;
        result["level"] = std::move(level);
    }

    auto selected = matjson::Value::object();
    selected["count"] = snapshot.selectedCount;
    auto byType = matjson::Value::array();
    for (auto const& [objectType, count] : snapshot.selectedByType) {
        auto entry = matjson::Value::object();
        entry["id"] = objectType;
        entry["count"] = count;
        byType.push(std::move(entry));
    }
    selected["by_type"] = std::move(byType);
    result["selected"] = std::move(selected);

    return result;
}

} // namespace gdai::editor
