#include "editor_state.hpp"

#include <Geode/binding/EditorUI.hpp>
#include <Geode/binding/GJGameLevel.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/binding/PlayLayer.hpp>

#include <cvolton.level-id-api/include/EditorIDs.hpp>

#include <algorithm>

using namespace geode::prelude;

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

    // `getAllObjects()` (backed by `GJBaseGameLayer::m_objects`) is the live
    // object list. Do NOT use `LevelEditorLayer::m_objectVector`: measured on a
    // level with a handful of objects it reported 10000 entries that were all
    // null, so it is a pre-sized table rather than the object collection.
    // This resolves the ambiguity flagged in SPEC.md 6.1.6.
    if (auto* objects = editor->getAllObjects()) {
        snapshot.objectCount = static_cast<int>(objects->count());
    }
    snapshot.editorLayer = static_cast<int>(editor->m_currentLayer);

    if (editor->m_level != nullptr) {
        snapshot.levelName = editor->m_level->m_levelName;
        // autoAssign = true, so the ID is stable even for a level that has not
        // been saved or uploaded yet. 0 means the API could not resolve one.
        snapshot.levelId = EditorIDs::getID(editor->m_level, true);
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

void dumpSaveStringSamples() {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr) {
        log::warn("save-string dump: no editor open");
        return;
    }

    // One sample per distinct object type, so the samples cover different
    // property sets rather than the same type repeated. Triggers are the most
    // informative, because they carry the largest key sets.
    auto* objects = editor->getAllObjects();
    if (objects == nullptr) {
        log::warn("save-string dump: editor reports no object list");
        return;
    }

    constexpr std::size_t kMaxSamples = 20;

    // Histogram first: it tells us definitively which object types are present,
    // so a missing sample can be attributed either to "not placed" or to "not
    // returned by getAllObjects()" rather than guessed at.
    std::vector<std::pair<int, int>> histogram;
    auto const total = objects->count();
    for (unsigned int i = 0; i < total; ++i) {
        auto* object = static_cast<GameObject*>(objects->objectAtIndex(i));
        if (object == nullptr) {
            continue;
        }
        auto const objectType = object->m_objectID;
        auto const found = std::find_if(
            histogram.begin(), histogram.end(),
            [objectType](auto const& entry) { return entry.first == objectType; }
        );
        if (found != histogram.end()) {
            ++found->second;
        } else {
            histogram.emplace_back(objectType, 1);
        }
    }
    for (auto const& [objectType, count] : histogram) {
        log::info("SAVESTRING-HISTOGRAM type={} count={}", objectType, count);
    }

    std::vector<int> seenTypes;
    auto sampled = 0;
    for (unsigned int i = 0; i < total && sampled < kMaxSamples; ++i) {
        auto* object = static_cast<GameObject*>(objects->objectAtIndex(i));
        if (object == nullptr) {
            continue;
        }
        auto const objectType = object->m_objectID;
        if (std::find(seenTypes.begin(), seenTypes.end(), objectType) != seenTypes.end()) {
            continue;
        }
        seenTypes.push_back(objectType);

        // Read-only: getSaveString serialises, it does not mutate.
        //
        // Both y values are logged deliberately. They differ by a constant
        // offset, because the save-string y is in level coordinates while
        // `getPositionY()` is the cocos position on the editor layer. That
        // offset is the `ground_y` relationship SPEC.md 7.5 needs, and it must
        // be measured rather than assumed -- a wrong baseline would silently
        // misplace every generated object.
        log::info(
            "SAVESTRING type={} uniqueID={} cocosY={:.1f} | {}",
            objectType, object->m_uniqueID, object->getPositionY(),
            object->getSaveString(editor)
        );
        ++sampled;
    }

    log::info(
        "save-string dump: {} distinct types sampled from {} objects",
        sampled, total
    );
}

} // namespace gdai::editor
