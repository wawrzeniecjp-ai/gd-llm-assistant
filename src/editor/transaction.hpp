#pragma once

#include <Geode/Result.hpp>

#include <matjson.hpp>

#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>
#include "properties.hpp"

namespace gdai::editor {

/// Records what a turn changed, so it can be undone (SPEC.md 6.1.5, D2).
///
/// **The mod does not register its edits on the editor's undo stack.** S3
/// measured that the default create path produces one undo entry *per object*
/// (5 objects -> 5 entries), and `UndoCommand` has no multi-object create
/// variant, so a grouped create entry cannot be built. A batch would therefore
/// need one Ctrl+Z per object, which is not usable.
///
/// So the mod keeps its own bookkeeping instead, and A2 applies to the panel's
/// Revert action rather than to Ctrl+Z. Two consequences worth knowing:
///
/// - **Our edits are invisible to Ctrl+Z.** Ctrl+Z always undoes the user's own
///   last edit, which is coherent for them but means Ctrl+Z and Revert do
///   different things. The UI has to say so.
/// - **The undo stack is a free change-detector.** Because our edits never touch
///   it, its depth only grows when the *user* edits. Comparing depth at the end
///   of a turn against depth at revert time answers "did anyone edit after us?"
///   without needing per-object change hooks (SPEC.md 6.1.7, a Phase 2 item).
class TransactionManager {
public:
    static TransactionManager& get();

    /// Opens a transaction. Fails with `TRANSACTION_ACTIVE` if one is open.
    geode::Result<std::string, std::string> begin(std::string_view label);

    /// Closes a transaction and reports what it changed.
    geode::Result<matjson::Value, std::string> end(std::string const& txnId);

    /// Undoes a transaction, newest change first.
    ///
    /// Fails with `TRANSACTION_NOT_REVERTIBLE` if the user has edited since it
    /// ended, and `OBJECT_NOT_FOUND` if the transaction is unknown.
    geode::Result<matjson::Value, std::string> revert(std::string const& txnId);

    /// Ends any open transaction without reverting it.
    ///
    /// Called when the connection drops or the editor closes (SPEC.md 6.1.5), so
    /// a half-finished turn does not leave a transaction dangling.
    void abortOpen(std::string_view reason);

    /// True while a transaction is open.
    [[nodiscard]] bool isOpen() const;

    /// The open transaction's id, or empty.
    [[nodiscard]] std::string const& openId() const;

    // -- recording, called by the job types as they mutate ---------------

    /// Notes an object this turn created.
    void recordCreated(std::int32_t uniqueId);

    /// Notes an object this turn removed, with its save string so it can be
    /// recreated. Call **before** removal, while the object is still valid.
    ///
    /// If the object was created by this same turn, the creation is cancelled
    /// rather than a deletion recorded, so that revert leaves it gone.
    void recordDeleted(std::string saveString, std::int32_t uniqueId);

    /// Notes properties this turn changed, with their values from before the
    /// change, so revert can restore them.
    ///
    /// Prior values are recorded **once per object**: a turn that modifies the
    /// same object three times must revert to the state before the first change,
    /// not the second. Later calls for an already-recorded object are ignored.
    void recordModified(
        std::int32_t uniqueId,
        std::vector<std::pair<std::string, PropertyValue>> priorValues
    );

    /// Notes a colour channel's colour before this turn changed it.
    ///
    /// Colour is **level** state, not object state (SPEC.md 7.5.2): one channel is
    /// shared by every object pointing at it. It therefore cannot ride in
    /// `recordModified`, which is keyed by object id and restored per object.
    ///
    /// First touch wins, for the same reason as properties.
    void recordColorChange(int channelId, int red, int green, int blue);

    /// True if this turn has already noted a change to the given object.
    [[nodiscard]] bool hasModified(std::int32_t uniqueId) const;

    /// Drops any recorded property changes for an object.
    ///
    /// Called when the object is **replaced** rather than modified -- a save-string
    /// rewrite removes the original and creates a new one, so recorded prior values are
    /// keyed to an id that will never be found again. Leaving them makes revert log a
    /// spurious "not found for property restore" and hides the real reason.
    void forgetModified(std::int32_t uniqueId);

private:
    TransactionManager() = default;

    struct Record {
        std::string id;
        std::string label;
        /// In creation order. Revert walks these backwards.
        std::vector<std::int32_t> created;
        /// Save strings of removed objects, in removal order.
        std::vector<std::string> deletedSaveStrings;
        /// Prior values per modified object, in first-touch order.
        std::vector<std::pair<std::int32_t, std::vector<std::pair<std::string, PropertyValue>>>>
            modified;
        /// Colour channels touched, with their colour before this turn.
        std::vector<std::pair<int, std::array<int, 3>>> colors;
        /// `m_undoObjects->count()` when the turn ended. The user editing after
        /// this point makes the turn non-revertible.
        int undoDepthAtEnd = 0;
        bool ended = false;
        bool reverted = false;
    };

    /// Completed transactions, keyed by id, for later revert.
    std::unordered_map<std::string, Record> m_finished;
    /// The open transaction, if any.
    Record m_open;
    bool m_hasOpen = false;
    std::uint64_t m_nextId = 1;
};

} // namespace gdai::editor
