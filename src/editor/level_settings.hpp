#pragma once

#include <Geode/Result.hpp>

#include <matjson.hpp>

#include <optional>
#include <string>

namespace gdai::editor {

/// Level-wide settings, as `LevelSettingsObject` holds them (SPEC.md 7.5.5).
///
/// All optional: a request sets only the fields it names, and the rest keep their
/// current values. One request rather than one per setting, because these live in a
/// single object, the game applies them together, and nobody changes them often.
struct LevelSettings {
    // -- visuals ---------------------------------------------------------
    /// Background pattern. Integer index into the game's background set.
    std::optional<int> background;
    /// Ground pattern, and the ground line style drawn with it.
    std::optional<int> ground;
    std::optional<int> groundLine;
    /// Middleground (third ground plane) pattern.
    std::optional<int> middleGround;

    // -- gameplay --------------------------------------------------------
    /// Starting gamemode (cube, ship, ball, ...), as `GameMode`'s integer value.
    std::optional<int> startMode;
    std::optional<bool> startMini;
    std::optional<bool> startDual;
    std::optional<bool> twoPlayerMode;
    std::optional<bool> platformerMode;
    std::optional<bool> reverseGameplay;
    std::optional<bool> rotateGameplay;
    /// Mirrors the level horizontally.
    std::optional<bool> mirrorMode;

    // -- audio -----------------------------------------------------------
    /// Seconds of offset between the level start and the song.
    std::optional<float> songOffset;
    // The song, which lives on `GJGameLevel` rather than on `LevelSettingsObject` even
    // though the user picks it in the same settings window.
    //
    // The pair's meaning is not what the names suggest, and was measured live after a
    // wrong first guess: `m_songID` is the **catalogue**, matching `SongSelectType`
    // (`Default = 0`, `Custom = 1`, `Default2 = 2`), and `m_audioTrack` is the index
    // into that catalogue. It is *not* a Newgrounds id.
    //
    // Read-only for the type. The custom catalogue means browsing, searching and
    // downloading from an external database -- a whole feature, and one that fails
    // silently on a bad id. Only the normal catalogue is writable here.
    std::optional<int> songType;
    /// Index into the normal catalogue.
    std::optional<int> audioTrack;
    /// The song's display name, when the game can resolve one. Read-only, and reported
    /// so the model can confirm with the user which song is in use.
    std::optional<std::string> songName;

    /// The speed the level starts at, as the simulator names it ("0.5x" .. "4x").
    /// Read-only: the sidecar's timeline and the level-start simulation need it
    /// (LEVEL-DESIGN-SPEC.md 8.1), and changing it is done with a speed portal.
    std::optional<std::string> startSpeed;
};

/// Reads the current settings. Main thread only.
geode::Result<LevelSettings> readLevelSettings();

/// The level's song for `get_level_music` (LEVEL-DESIGN-SPEC.md 8.2). Main thread only.
///
/// Reports what the game already knows without loading audio: the catalogue and track,
/// the offset, the name, and for an official track the file it plays from. `length_ms`
/// and `guidelines` are null until spikes S-L7 and S-L6 find where they come from.
geode::Result<matjson::Value> readLevelMusic();

/// Applies `settings`, then asks the editor to refresh. Main thread only.
///
/// Uses `LevelEditorLayer::levelSettingsUpdated()` rather than recreating the
/// background and ground by hand. That virtual is what the editor's own settings popup
/// calls when it closes, so it is the game's path for "these values changed, re-apply
/// everything that depends on them" -- background, ground, ground line, middleground and
/// colours all follow from it. Doing the recreation here instead would mean reproducing
/// node positions, scales and draw order that the game already gets right.
///
/// Settings are level state, not object state, so nothing here is recorded in a
/// transaction: Revert restores a turn's *edits*, and a settings change is not an edit to
/// an object. Reported to the caller so it can say so.
geode::Result<LevelSettings> writeLevelSettings(LevelSettings const& settings);

/// Serialises settings for the protocol. Only fields that were set are emitted.
matjson::Value levelSettingsToJson(LevelSettings const& settings);

/// Parses a `set_level_settings` payload into a partial `LevelSettings`.
///
/// Every field is optional, and an unknown field is refused rather than ignored: a
/// request that silently drops half its keys reports success for work it did not do.
/// Errors carry a protocol code prefix, which the bridge splits back out.
geode::Result<LevelSettings, std::string> parseLevelSettings(matjson::Value const& json);

} // namespace gdai::editor
