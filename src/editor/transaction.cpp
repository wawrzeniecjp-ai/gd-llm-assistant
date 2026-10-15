#include "transaction.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>

#include <matjson.hpp>

using namespace geode::prelude;

namespace gdai::editor {
namespace {

/// Editor and Cocos2d access is main-thread only (SPEC.md 6.1.3 rule 2).
std::int32_t objectCount() {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr) {
        return 0;
    }
    auto* objects = editor->getAllObjects();
    return objects == nullptr ? 0 : static_cast<std::int32_t>(objects->count());
}

/// Depth of the editor's undo stack.
///
/// Only the user's edits add to it, because the mod's do not (see the header).
int undoDepth() {
    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr || editor->m_undoObjects == nullptr) {
        return 0;
    }
    return static_cast<int>(editor->m_undoObjects->count());
}

} // namespace

TransactionManager& TransactionManager::get() {
    static TransactionManager instance;
    return instance;
}

bool TransactionManager::isOpen() const {
    return m_hasOpen;
}

std::string const& TransactionManager::openId() const {
    static std::string const empty;
    return m_hasOpen ? m_open.id : empty;
}

geode::Result<std::string, std::string> TransactionManager::begin(std::string_view label) {
    if (m_hasOpen) {
        return Err(fmt::format(
            "TRANSACTION_ACTIVE: transaction '{}' is still open", m_open.id
        ));
    }

    m_open = Record {};
    m_open.id = fmt::format("x-{}", m_nextId++);
    m_open.label = std::string(label);
    m_hasOpen = true;

    log::info("transaction {} opened ({})", m_open.id, m_open.label);
    return Ok(m_open.id);
}

void TransactionManager::recordCreated(std::int32_t uniqueId) {
    if (m_hasOpen) {
        m_open.created.push_back(uniqueId);
    }
}

void TransactionManager::recordDeleted(std::string saveString) {
    if (m_hasOpen) {
        m_open.deletedSaveStrings.push_back(std::move(saveString));
    }
}

bool TransactionManager::hasModified(std::int32_t uniqueId) const {
    if (!m_hasOpen) {
        return false;
    }
    return std::any_of(m_open.modified.begin(), m_open.modified.end(),
        [uniqueId](auto const& entry) { return entry.first == uniqueId; });
}

void TransactionManager::recordModified(
    std::int32_t uniqueId,
    std::vector<std::pair<std::string, PropertyValue>> priorValues
) {
    if (!m_hasOpen) {
        return;
    }
    // First touch wins: reverting must restore the state before the turn, not
    // before the most recent of several changes to the same object.
    if (hasModified(uniqueId)) {
        return;
    }
    m_open.modified.emplace_back(uniqueId, std::move(priorValues));
}

geode::Result<matjson::Value, std::string> TransactionManager::end(std::string const& txnId) {
    if (!m_hasOpen || m_open.id != txnId) {
        return Err(fmt::format("INVALID_REQUEST: no open transaction '{}'", txnId));
    }

    // Captured at end-of-turn: any growth after this is the user editing, which
    // is what makes the turn non-revertible.
    m_open.undoDepthAtEnd = undoDepth();
    m_open.ended = true;

    auto summary = matjson::Value::object();
    summary["created"] = static_cast<int>(m_open.created.size());
    summary["modified"] = static_cast<int>(m_open.modified.size());
    summary["deleted"] = static_cast<int>(m_open.deletedSaveStrings.size());

    log::info(
        "transaction {} ended: {} created, {} modified, {} deleted (undo depth {})",
        m_open.id, m_open.created.size(), m_open.modified.size(),
        m_open.deletedSaveStrings.size(), m_open.undoDepthAtEnd
    );

    auto result = matjson::Value::object();
    result["txn_id"] = m_open.id;
    result["summary"] = std::move(summary);

    m_finished[m_open.id] = std::move(m_open);
    m_open = Record {};
    m_hasOpen = false;

    return Ok(std::move(result));
}

void TransactionManager::abortOpen(std::string_view reason) {
    if (!m_hasOpen) {
        return;
    }
    // Ended but not revertible-by-UI: the turn did not finish, so the sidecar
    // marks it incomplete and the user reverts with Ctrl+Z if they want to.
    log::warn("transaction {} abandoned ({}): {} created, {} deleted", 
        m_open.id, reason, m_open.created.size(), m_open.deletedSaveStrings.size());
    m_open = Record {};
    m_hasOpen = false;
}

geode::Result<matjson::Value, std::string> TransactionManager::revert(std::string const& txnId) {
    auto found = m_finished.find(txnId);
    if (found == m_finished.end()) {
        return Err(fmt::format("OBJECT_NOT_FOUND: unknown transaction '{}'", txnId));
    }
    auto& record = found->second;

    if (record.reverted) {
        return Err(fmt::format("TRANSACTION_NOT_REVERTIBLE: '{}' was already reverted", txnId));
    }

    // The user editing after the turn makes reverting unsafe: our inverse would
    // apply on top of changes that assume our edits are still present.
    auto const currentDepth = undoDepth();
    if (currentDepth > record.undoDepthAtEnd) {
        log::info(
            "refusing to revert {}: undo depth {} exceeds {} recorded at turn end",
            txnId, currentDepth, record.undoDepthAtEnd
        );
        return Err("TRANSACTION_NOT_REVERTIBLE: the level was edited after this turn");
    }

    auto* editor = LevelEditorLayer::get();
    if (editor == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }

    auto const before = objectCount();

    // Reverse order, so a turn that created then deleted the same object unwinds
    // in the opposite sequence to how it was applied.
    auto removed = 0;
    for (auto it = record.created.rbegin(); it != record.created.rend(); ++it) {
        // Re-resolve by unique ID rather than holding a pointer from earlier in
        // the turn (SPEC.md 6.1.3 rule 5). Null means it is already gone, which
        // happens if the user deleted it -- not an error.
        if (auto* object = editor->findGameObject(*it)) {
            editor->removeObject(object, true);
            ++removed;
        }
    }

    auto restored = 0;
    for (auto it = record.deletedSaveStrings.rbegin();
         it != record.deletedSaveStrings.rend(); ++it) {
        // Recreate from the level-format string, which carries every property.
        // `noUndo = true` keeps the restore off the user's undo stack; note the
        // recreated object gets a NEW unique ID, so any identifier the sidecar
        // holds for it is stale after a revert.
        if (editor->createObjectsFromString(*it, true, true) != nullptr) {
            ++restored;
        }
    }

    // Prior values are written back through the same routing as the original
    // modification, so a read -> modify -> revert round trip is symmetric.
    auto revertedProperties = 0;
    for (auto it = record.modified.rbegin(); it != record.modified.rend(); ++it) {
        auto const& [uniqueId, priorValues] = *it;
        auto* object = editor->findGameObject(uniqueId);
        if (object == nullptr || object->m_uniqueID != uniqueId) {
            // Gone, or the ID now belongs to a different object after a restore.
            continue;
        }
        for (auto const& [name, value] : priorValues) {
            if (writeProperty(object, name, value)) {
                ++revertedProperties;
            } else {
                log::warn("revert {}: could not restore '{}' on object {}", txnId, name, uniqueId);
            }
        }
    }

    record.reverted = true;

    log::info(
        "transaction {} reverted: {} of {} creations removed, {} of {} deletions restored, "
        "{} properties restored",
        txnId, removed, record.created.size(), restored, record.deletedSaveStrings.size(),
        revertedProperties
    );

    auto result = matjson::Value::object();
    result["reverted"] = true;
    result["removed"] = removed;
    result["restored"] = restored;
    result["properties_restored"] = revertedProperties;
    result["object_count_before"] = before;
    result["object_count_after"] = objectCount();
    return Ok(std::move(result));
}

} // namespace gdai::editor
