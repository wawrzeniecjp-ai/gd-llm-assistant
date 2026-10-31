#include "save_properties.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>

#include "transaction.hpp"

#include <algorithm>
#include <charconv>

using namespace geode::prelude;

namespace gdai::editor {
namespace {

/// Resolves a unique ID, re-checking it.
///
/// A recycled slot can hand back an object whose ID no longer matches, so the lookup is
/// verified rather than trusted (SPEC.md 6.1.3 rule 5).
GameObject* resolve(LevelEditorLayer* editor, std::int32_t uniqueId) {
    auto* object = editor->findGameObject(uniqueId);
    if (object == nullptr || object->m_uniqueID != uniqueId) {
        return nullptr;
    }
    return object;
}

} // namespace

std::vector<SaveEntry> parseSaveString(std::string const& saveString) {
    std::vector<SaveEntry> entries;

    std::size_t start = 0;
    while (start < saveString.size()) {
        auto const comma = saveString.find(',', start);
        auto const keyText = saveString.substr(
            start, comma == std::string::npos ? std::string::npos : comma - start
        );
        if (comma == std::string::npos) {
            break;  // a key with no value: incomplete, dropped
        }

        auto const valueStart = comma + 1;
        auto const nextComma = saveString.find(',', valueStart);
        auto const value = saveString.substr(
            valueStart,
            nextComma == std::string::npos ? std::string::npos : nextComma - valueStart
        );

        auto parsed = 0;
        auto const [end, error] = std::from_chars(
            keyText.data(), keyText.data() + keyText.size(), parsed
        );
        if (error == std::errc() && end == keyText.data() + keyText.size()) {
            entries.push_back(SaveEntry { parsed, value });
        } else {
            // A non-numeric key means the string is not the format being assumed. Stop
            // rather than pushing garbage: half a save string is more useful than one
            // with nonsense keys in it.
            log::warn("save string: unparseable key '{}', stopping", keyText);
            break;
        }

        if (nextComma == std::string::npos) {
            break;
        }
        start = nextComma + 1;
    }

    return entries;
}

std::string joinSaveString(std::vector<SaveEntry> const& entries) {
    std::string out;
    for (auto const& entry : entries) {
        if (!out.empty()) {
            out.push_back(',');
        }
        out += fmt::format("{},{}", entry.key, entry.value);
    }
    return out;
}

std::optional<std::string> saveValue(std::vector<SaveEntry> const& entries, int key) {
    auto const found = std::find_if(
        entries.begin(), entries.end(), [key](SaveEntry const& entry) { return entry.key == key; }
    );
    if (found == entries.end()) {
        return std::nullopt;
    }
    return found->value;
}

void setSaveValue(std::vector<SaveEntry>& entries, int key, std::string value) {
    auto const found = std::find_if(
        entries.begin(), entries.end(), [key](SaveEntry const& entry) { return entry.key == key; }
    );
    if (found != entries.end()) {
        found->value = std::move(value);
        return;
    }
    entries.push_back(SaveEntry { key, std::move(value) });
}

geode::Result<std::vector<SaveEntry>, std::string> readSaveEntries(std::int32_t uniqueId) {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }
    auto* object = resolve(editor, uniqueId);
    if (object == nullptr) {
        return Err(fmt::format("OBJECT_NOT_FOUND: no object with id {}", uniqueId));
    }

    auto const saveString = object->getSaveString(editor);
    if (saveString.empty()) {
        // Distinct from "no properties": it means this type does not serialise, which
        // the model should hear as "cannot be edited this way" rather than as an empty
        // object.
        return Err(fmt::format(
            "UNSUPPORTED_PROPERTY: object {} produced no save string (type {} does not "
            "serialise)",
            uniqueId, object->m_objectID
        ));
    }
    return Ok(parseSaveString(saveString));
}

geode::Result<std::int32_t, std::string> rewriteSaveProperty(
    std::int32_t uniqueId, int key, std::string const& value
) {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }
    auto* object = resolve(editor, uniqueId);
    if (object == nullptr) {
        return Err(fmt::format("OBJECT_NOT_FOUND: no object with id {}", uniqueId));
    }
    // Keys 1, 2 and 3 are the type id and the position, and the position is the one that
    // would silently move the object if written in the wrong coordinate space: the save
    // string stores level Y while every other interface here uses Cocos Y. Refusing them
    // points the caller at `modify_objects`, which gets the conversion right.
    if (key == 1 || key == 2 || key == 3) {
        return Err(fmt::format(
            "INVALID_REQUEST: key {} is the object type or position -- use 'id', 'x' or "
            "'y' through modify_objects instead, which converts coordinates correctly",
            key
        ));
    }

    auto const original = object->getSaveString(editor);
    if (original.empty()) {
        return Err(fmt::format(
            "UNSUPPORTED_PROPERTY: object {} produced no save string (type {} does not "
            "serialise)",
            uniqueId, object->m_objectID
        ));
    }

    auto entries = parseSaveString(original);
    if (entries.empty()) {
        return Err(fmt::format("INTERNAL_ERROR: could not parse the save string of {}", uniqueId));
    }

    auto const before = saveValue(entries, key);
    setSaveValue(entries, key, value);
    auto const rewritten = joinSaveString(entries);

    if (rewritten == original) {
        return Ok(uniqueId);
    }

    auto& transactions = TransactionManager::get();

    // Recorded before the removal, and in this order, so revert unwinds correctly:
    // `recordDeleted` cancels an earlier creation of the same id (which is right -- the
    // object's whole existence began this turn, so revert should leave nothing behind),
    // and then the replacement is recorded as created so revert removes it.
    transactions.recordDeleted(original, uniqueId);
    // Any property changes recorded for the old id describe an object that is about to
    // stop existing; leaving them would make revert log a spurious "not found".
    transactions.forgetModified(uniqueId);

    editor->removeObject(object, false);

    auto* created = editor->createObjectsFromString(rewritten, true, true);
    if (created == nullptr || created->count() == 0) {
        // The original is already gone, so this is not recoverable here. Reported
        // explicitly rather than returning a failure the caller might retry: a retry
        // would target an id that no longer exists and report OBJECT_NOT_FOUND, hiding
        // what actually happened.
        log::error(
            "save property rewrite of {} (key {}) removed the object but could not "
            "recreate it from '{}'",
            uniqueId, key, rewritten
        );
        return Err(fmt::format(
            "INTERNAL_ERROR: object {} was removed but the rewritten save string could "
            "not be turned back into an object. The original save string was '{}'",
            uniqueId, original
        ));
    }

    auto* replacement = typeinfo_cast<GameObject*>(created->objectAtIndex(0));
    if (replacement == nullptr) {
        log::error("save property rewrite of {} produced a non-GameObject", uniqueId);
        return Err(fmt::format("INTERNAL_ERROR: object {} was replaced by something invalid", uniqueId));
    }

    auto const newId = replacement->m_uniqueID;
    transactions.recordCreated(newId);

    log::info(
        "save property: object {} -> {} (key {}{})",
        uniqueId, newId, key, before ? fmt::format(" was '{}'", *before) : ", newly set"
    );
    return Ok(newId);
}

} // namespace gdai::editor
