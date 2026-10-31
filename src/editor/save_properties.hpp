#pragma once

#include <Geode/Result.hpp>

#include <matjson.hpp>

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

class GameObject;

namespace gdai::editor {

/// One `key,value` pair of an object's save string.
///
/// The value stays a **string** on purpose. Save strings encode their own types --
/// integers, floats, dot-separated group lists, `a`-separated HSV quadruples, base64
/// text -- and parsing them into typed values here would mean re-deriving a format the
/// game already defines. The sidecar's generated property table (SPEC.md 7.5.3) knows
/// which key means what and what type it holds, so the conversion happens there, once.
struct SaveEntry {
    int key = 0;
    std::string value;
};

/// Splits an object's save string into pairs (SPEC.md 7.5.4).
///
/// The format is `1,<objectID>,2,<x>,3,<y>,...` -- alternating key and value, comma
/// separated. A trailing comma or an odd count is tolerated by dropping the incomplete
/// pair, because a malformed tail is a reason to skip one entry rather than to refuse
/// to read an object at all.
std::vector<SaveEntry> parseSaveString(std::string const& saveString);

/// Serialises pairs back to a save string.
std::string joinSaveString(std::vector<SaveEntry> const& entries);

/// The value stored under `key`, or nullopt.
std::optional<std::string> saveValue(std::vector<SaveEntry> const& entries, int key);

/// Sets `key` to `value`, replacing it in place or appending it.
///
/// Appending is deliberate: an absent key is how the game writes "this property has its
/// default", and adding it is exactly how a property gets switched on. The alternative
/// -- refusing to write a key the object does not already have -- would make it
/// impossible to set anything on an object whose defaults are implicit.
void setSaveValue(std::vector<SaveEntry>& entries, int key, std::string value);

/// Reads every save-string property of one object. Main thread only.
///
/// Returns an empty result with `OBJECT_NOT_FOUND` when the id is unknown. Used by
/// `get_save_properties` so the model can see properties this mod has no name for --
/// which is the whole point of the generated table: it can name them, and this can
/// reach them.
geode::Result<std::vector<SaveEntry>, std::string> readSaveEntries(std::int32_t uniqueId);

/// Rewrites one object's save string so `key` becomes `value`, by rebuilding it.
///
/// **The object is replaced, and its unique ID changes.** That is not a shortcut: the
/// save string is the only interface that reaches properties the mod does not model,
/// and the game can only turn a save string into an object by creating one. The caller
/// is told the new ID so it can keep working with the object.
///
/// The rebuild is recorded in the open transaction as a delete of the original plus a
/// creation of the replacement, which is exactly what happened -- so Revert puts the
/// original back, with its original properties.
///
/// `noUndo = true` on creation and `false` on removal, matching the measured rules in
/// this project: creation with `noUndo = true` is safe, removal with it corrupts the
/// heap (`kTestDeleteObjects` in handler_verification.cpp).
geode::Result<std::int32_t, std::string> rewriteSaveProperty(
    std::int32_t uniqueId, int key, std::string const& value
);

} // namespace gdai::editor
