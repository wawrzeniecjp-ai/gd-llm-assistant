#include "editor_state.hpp"

#include <Geode/binding/EditorUI.hpp>
#include <Geode/binding/GJGameLevel.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/binding/PlayLayer.hpp>

#include <algorithm>

#include "transaction.hpp"

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
        // `levelId` is deliberately left 0, i.e. "unknown".
        //
        // It used to come from `cvolton.level-id-api`, which is now dropped: its
        // headers are built on `GEODE_EVENT_EXPORT_*` macros, and Geode v6 reworked
        // the event system ("V6 event cleanup"), so the header no longer compiles
        // against the v6 SDK. That is a third-party mod needing a v6 port, not
        // something this project can fix, and hanging the whole build on it to gain
        // a nicer session key was the wrong trade.
        //
        // Nothing is lost functionally: `Bridge::buildHello` already falls back to
        // the level name when the ID is unavailable, which SPEC.md 6.2.5 lists as the
        // default session key. The cost is the known collision/rename caveat there.
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

namespace {

/// Ordered (key, value) pairs from a save string, values kept as text so no
/// float/int round-tripping can disguise a change.
///
/// Named `splitSaveString` rather than `parseProperties` to stay distinct from
/// the protocol-level parser in properties.hpp, which does a different job.
std::vector<std::pair<std::string, std::string>> splitSaveString(std::string_view text) {
    std::vector<std::pair<std::string, std::string>> pairs;
    std::size_t position = 0;
    while (position < text.size()) {
        auto const comma = text.find(',', position);
        if (comma == std::string_view::npos) {
            break;
        }
        auto const next = text.find(',', comma + 1);
        auto const key = std::string(text.substr(position, comma - position));
        auto const value = std::string(text.substr(
            comma + 1, next == std::string_view::npos ? std::string_view::npos : next - comma - 1
        ));
        pairs.emplace_back(key, value);
        position = next == std::string_view::npos ? text.size() : next + 1;
    }
    return pairs;
}

/// The keys that differ between a baseline and a perturbed save string.
std::string describeChanges(
    std::vector<std::pair<std::string, std::string>> const& before,
    std::vector<std::pair<std::string, std::string>> const& after
) {
    std::string out;
    for (auto const& [key, value] : after) {
        auto const found = std::find_if(
            before.begin(), before.end(),
            [&key](auto const& entry) { return entry.first == key; }
        );
        if (found == before.end()) {
            fmt::format_to(std::back_inserter(out), " key {}:= {} (added)", key, value);
        } else if (found->second != value) {
            fmt::format_to(std::back_inserter(out), " key {}: {} -> {}", key, found->second, value);
        }
    }
    for (auto const& [key, value] : before) {
        auto const stillThere = std::any_of(
            after.begin(), after.end(),
            [&key](auto const& entry) { return entry.first == key; }
        );
        if (!stillThere) {
            fmt::format_to(std::back_inserter(out), " key {}: {} (removed)", key, value);
        }
    }
    return out.empty() ? std::string(" (no change)") : out;
}

/// A standard block: the simplest object, so the baseline key set is minimal and
/// any change is attributable to the perturbation rather than to type defaults.
constexpr int kProbeType = 1;
constexpr float kProbeX = 300.f;
constexpr float kProbeY = 300.f;

struct Probe {
    char const* property;
    void (*apply)(GameObject*);
};

} // namespace

void runDifferentialProbe() {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr) {
        log::warn("differential probe: no editor open");
        return;
    }

    // Each probe gets a fresh object, so every baseline is identical and no
    // restore logic (and therefore no getter semantics) is relied upon.
    //
    // Round 1 result: 9 of these 12 confirmed a key. `setScaleX`/`setScaleY` and
    // `setOpacity` produced no change. The scale pair is re-probed below via
    // fields, because the save string may read `m_scaleX` rather than the cocos
    // node scale the setters touch. `setOpacity` on a standard block is expected
    // to do nothing: key 35 is a trigger property, not a block one.
    static const Probe kProbes[] = {
        { "x", [](GameObject* o) { o->setPosition({ kProbeX + 30.f, kProbeY }); } },
        { "y", [](GameObject* o) { o->setPosition({ kProbeX, kProbeY + 30.f }); } },
        { "rotation", [](GameObject* o) { o->setRotation(45.f); } },
        { "flip_x", [](GameObject* o) { o->setFlipX(true); } },
        { "flip_y", [](GameObject* o) { o->setFlipY(true); } },
        { "groups", [](GameObject* o) { o->addToGroup(7); } },
        { "z_order", [](GameObject* o) { o->m_zOrder = 5; } },
        { "editor_layer", [](GameObject* o) { o->m_editorLayer = 2; } },
        // Arbitrary but distinct layer value; the enum's numbering is not
        // assumed, only that a different value changes whatever key it feeds.
        { "z_layer", [](GameObject* o) { o->m_zLayer = static_cast<ZLayer>(9); } },
        // Re-probes for the three that did not respond to a setter.
        { "scale_x_setter", [](GameObject* o) { o->setScaleX(1.5f); } },
        { "scale_x_field", [](GameObject* o) { o->m_scaleX = 1.5f; } },
        { "scale_y_field", [](GameObject* o) { o->m_scaleY = 1.5f; } },
        { "scale_both_setter", [](GameObject* o) { o->setScale(1.5f); } },
        // Colour channels, which SPEC.md 7.5 exposes as color_channel_1/2.
        { "color_channel_1", [](GameObject* o) {
            if (o->m_baseColor != nullptr) { o->m_baseColor->m_colorID = 4; }
        } },
        { "color_channel_2", [](GameObject* o) {
            if (o->m_detailColor != nullptr) { o->m_detailColor->m_colorID = 5; }
        } },
    };

    log::info("differential probe: starting ({} properties)", std::size(kProbes));

    for (auto const& probe : kProbes) {
        // noUndo = true: the scratch object must not enter the undo stack.
        auto* object = editor->createObject(
            kProbeType, cocos2d::CCPoint { kProbeX, kProbeY }, true
        );
        if (object == nullptr) {
            log::warn("differential probe: could not create a scratch object for {}", probe.property);
            continue;
        }

        auto const before = splitSaveString(object->getSaveString(editor));
        probe.apply(object);
        auto const after = splitSaveString(object->getSaveString(editor));

        log::info(
            "DIFF {} |{}",
            probe.property,
            describeChanges(before, after)
        );

        // Leave the level as found. noUndo = true for symmetry with creation.
        editor->removeObject(object, true);
    }

    log::info("differential probe: done; scratch objects removed");
}

void runUndoProbe() {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr) {
        log::warn("undo probe: no editor open");
        return;
    }

    constexpr int kBatch = 5;
    constexpr float kProbeY = 450.f;

    auto const countObjects = [editor] {
        auto* objects = editor->getAllObjects();
        return objects == nullptr ? 0 : static_cast<int>(objects->count());
    };

    // Objects created with an undo entry would allow Ctrl+Z to remove the probe's
    // own work, so start from a clean stack. Clear both halves.
    editor->m_undoObjects->removeAllObjects();
    editor->m_redoObjects->removeAllObjects();

    auto const before = countObjects();
    log::info("undo probe: starting with {} objects, creating {} with undo enabled", before, kBatch);

    std::vector<GameObject*> created;
    for (auto i = 0; i < kBatch; ++i) {
        // noUndo = false: this is the case under test.
        auto* object = editor->createObject(
            1, cocos2d::CCPoint { 100.f + static_cast<float>(i) * 30.f, kProbeY }, false
        );
        if (object != nullptr) {
            created.push_back(object);
        }
    }

    auto const afterCreate = countObjects();
    auto const undoEntries = static_cast<int>(editor->m_undoObjects->count());
    log::info(
        "undo probe: {} objects created ({} now present), {} undo entries on the stack",
        static_cast<int>(created.size()), afterCreate, undoEntries
    );

    if (undoEntries > 0) {
        // The decisive measurement. If one undo removes all kBatch objects, then
        // a batch is already one undo entry and A2 works via Ctrl+Z. If it
        // removes one, grouping has to be constructed explicitly.
        editor->undoLastAction();
        auto const afterUndo = countObjects();
        log::info(
            "UNDO-PROBE-RESULT: one undo removed {} of {} objects ({} remaining)",
            afterCreate - afterUndo, kBatch, afterUndo
        );

        // Whether undo moved entries to the redo stack, which is what would let
        // `revert_transaction` be implemented as a targeted undo.
        log::info(
            "undo probe: undo stack now {}, redo stack {}",
            static_cast<int>(editor->m_undoObjects->count()),
            static_cast<int>(editor->m_redoObjects->count())
        );
    } else {
        log::warn(
            "UNDO-PROBE-RESULT: createObject added no undo entries, so undo grouping "
            "cannot be built on it"
        );
    }

    // Clean up: redo restores the batch if undo removed it, then remove anything
    // still present so the level is left as found. `noUndo = true` keeps this
    // cleanup off the stack, but the stack is cleared anyway so no probe entry
    // can be undone later.
    if (editor->m_redoObjects->count() > 0) {
        editor->redoLastAction();
    }
    for (auto* object : created) {
        // The object may already be gone; findGameObject resolves safely rather
        // than dereferencing a dangling pointer (SPEC.md 6.1.3 rule 5).
        if (auto* live = editor->findGameObject(object->m_uniqueID)) {
            editor->removeObject(live, true);
        }
    }

    editor->m_undoObjects->removeAllObjects();
    editor->m_redoObjects->removeAllObjects();

    log::info("undo probe: done; {} objects now present (started with {})", countObjects(), before);
}

void runTransactionProbe() {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr) {
        log::warn("transaction probe: no editor open");
        return;
    }

    constexpr int kBatch = 3;
    constexpr float kProbeY = 750.f;

    auto const countObjects = [editor] {
        auto* objects = editor->getAllObjects();
        return objects == nullptr ? 0 : static_cast<int>(objects->count());
    };

    auto const before = countObjects();
    log::info("transaction probe: starting with {} objects", before);

    auto& transactions = TransactionManager::get();
    if (transactions.isOpen()) {
        // Left over from an abandoned run; end it so this probe can proceed.
        transactions.abortOpen("transaction probe");
    }

    auto opened = transactions.begin("probe");
    if (!opened) {
        log::warn("transaction probe: begin failed: {}", opened.unwrapErr());
        return;
    }
    auto const txnId = opened.unwrap();
    log::info("transaction probe: opened {}", txnId);

    // Create with noUndo, which keeps this turn off the user's undo stack and is
    // the behaviour the real create_objects job will use (Option A, D2).
    auto created = 0;
    for (auto i = 0; i < kBatch; ++i) {
        auto* object = editor->createObject(
            1, cocos2d::CCPoint { 100.f + static_cast<float>(i) * 30.f, kProbeY }, true
        );
        if (object != nullptr) {
            transactions.recordCreated(object->m_uniqueID);
            ++created;
        }
    }
    auto const afterCreate = countObjects();
    log::info(
        "transaction probe: recorded {} creations, {} objects now present",
        created, afterCreate
    );

    auto ended = transactions.end(txnId);
    if (!ended) {
        log::warn("transaction probe: end failed: {}", ended.unwrapErr());
        return;
    }
    log::info(
        "transaction probe: ended, summary {}",
        ended.unwrap()["summary"].dump(0)
    );

    // The measurement that matters: does the bookkeeping restore the level?
    auto reverted = transactions.revert(txnId);
    if (!reverted) {
        log::warn("transaction probe: revert failed: {}", reverted.unwrapErr());
        return;
    }
    auto const afterRevert = countObjects();
    log::info(
        "TRANSACTION-PROBE-RESULT: {} -> {} -> {} objects (started {}, expected {})",
        before, afterCreate, afterRevert, before, before
    );
    log::info(
        "transaction probe: {}",
        afterRevert == before ? "PASS (level restored)" : "FAIL (count did not return)"
    );

    // Reverting twice must be refused, which is what the UI relies on to hide the
    // Revert action once it has been used.
    if (auto second = transactions.revert(txnId); !second) {
        log::info("transaction probe: second revert correctly refused: {}", second.unwrapErr());
    } else {
        log::warn("transaction probe: second revert was allowed, which it should not be");
    }
}

void runUndoShapeProbe() {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr) {
        log::warn("undo-shape probe: no editor open");
        return;
    }

    constexpr int kBatch = 3;
    constexpr float kProbeY = 600.f;

    auto const countObjects = [editor] {
        auto* objects = editor->getAllObjects();
        return objects == nullptr ? 0 : static_cast<int>(objects->count());
    };

    editor->m_undoObjects->removeAllObjects();
    editor->m_redoObjects->removeAllObjects();

    auto const before = countObjects();
    log::info("undo-shape probe: starting with {} objects", before);

    // Created with undo enabled, which the first probe already proved is safe.
    // Nothing is constructed by hand here: the point is to observe the entries
    // the game itself produces.
    std::vector<GameObject*> created;
    for (auto i = 0; i < kBatch; ++i) {
        auto* object = editor->createObject(
            1, cocos2d::CCPoint { 100.f + static_cast<float>(i) * 30.f, kProbeY }, false
        );
        if (object != nullptr) {
            created.push_back(object);
        }
    }

    auto const entries = static_cast<int>(editor->m_undoObjects->count());
    log::info("undo-shape probe: {} objects created, {} stack entries", kBatch, entries);

    // The decisive read. Each element is either a single UndoObject (one entry
    // per action) or a CCArray of them (one entry per group). Which one it is
    // determines whether grouping needs a wrapper array or a multi-object entry.
    for (auto i = 0; i < entries && i < 3; ++i) {
        auto* element = editor->m_undoObjects->objectAtIndex(static_cast<unsigned>(i));
        if (element == nullptr) {
            log::info("  entry {}: null", i);
            continue;
        }

        if (auto* array = dynamic_cast<cocos2d::CCArray*>(element)) {
            log::info(
                "  entry {}: CCArray with {} element(s)", i,
                static_cast<int>(array->count())
            );
            if (array->count() > 0) {
                auto* inner = array->objectAtIndex(0);
                log::info(
                    "    inner[0]: {} (UndoObject={}, CCArray={})",
                    inner == nullptr ? "null" : "non-null",
                    dynamic_cast<UndoObject*>(inner) != nullptr,
                    dynamic_cast<cocos2d::CCArray*>(inner) != nullptr
                );
            }
        } else if (auto* undo = dynamic_cast<UndoObject*>(element)) {
            log::info(
                "  entry {}: UndoObject command={} objects={}",
                i, static_cast<int>(undo->m_command),
                undo->m_objects == nullptr ? -1 : static_cast<int>(undo->m_objects->count())
            );
        } else {
            log::info("  entry {}: neither CCArray nor UndoObject", i);
        }
    }

    // Clean up without ever calling handleAction: remove the objects directly and
    // clear both stacks. This is why the probe cannot crash the game the way the
    // previous attempt did.
    for (auto* object : created) {
        if (auto* live = editor->findGameObject(object->m_uniqueID)) {
            editor->removeObject(live, true);
        }
    }
    editor->m_undoObjects->removeAllObjects();
    editor->m_redoObjects->removeAllObjects();

    log::info(
        "undo-shape probe: done; {} objects now present (started with {})",
        countObjects(), before
    );
}

} // namespace gdai::editor
