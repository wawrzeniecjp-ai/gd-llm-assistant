#include "level_settings.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/binding/GJGameLevel.hpp>
#include <Geode/binding/LevelSettingsObject.hpp>
#include <Geode/binding/LevelTools.hpp>

#include <algorithm>
#include <filesystem>
#include <system_error>

using namespace geode::prelude;

namespace gdai::editor {
namespace {

LevelSettingsObject* settingsObject() {
    auto* editor = LevelEditorLayer::get();
    return editor == nullptr ? nullptr : editor->m_levelSettings;
}

/// A closed range check, reported rather than clamped.
///
/// Clamping would apply a value the caller did not ask for and report success, which is
/// the failure mode this project keeps running into: a write that "works" and produces
/// something else. Refusing names the field and the range instead.
geode::Result<> inRange(char const* field, int value, int low, int high) {
    if (value < low || value > high) {
        return Err(fmt::format(
            "INVALID_REQUEST: '{}' must be between {} and {}, got {}", field, low, high, value
        ));
    }
    return Ok();
}

} // namespace

geode::Result<LevelSettings> readLevelSettings() {
    auto* settings = settingsObject();
    if (settings == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }

    LevelSettings current;
    current.background = settings->m_backgroundIndex;
    current.ground = settings->m_groundIndex;
    current.groundLine = settings->m_groundLineIndex;
    current.middleGround = settings->m_middleGroundIndex;
    current.startMode = settings->m_startMode;
    current.startMini = settings->m_startMini;
    current.startDual = settings->m_startDual;
    current.twoPlayerMode = settings->m_twoPlayerMode;
    current.platformerMode = settings->m_platformerMode;
    current.reverseGameplay = settings->m_reverseGameplay;
    current.rotateGameplay = settings->m_rotateGameplay;
    current.mirrorMode = settings->m_mirrorMode;
    current.songOffset = settings->m_songOffset;
    // `Speed`'s values: 0 normal (1x), 1 slow (0.5x), 2 fast, 3 faster, 4 fastest.
    switch (static_cast<int>(settings->m_startSpeed)) {
        case 1: current.startSpeed = "0.5x"; break;
        case 2: current.startSpeed = "2x"; break;
        case 3: current.startSpeed = "3x"; break;
        case 4: current.startSpeed = "4x"; break;
        default: current.startSpeed = "1x"; break;
    }
    // The song lives on the level, not the settings object, even though the user picks
    // it in the settings window.
    if (auto* level = settings->m_level) {
        current.songType = level->m_songID;
        current.audioTrack = level->m_audioTrack;
        auto const name = level->getSongName();
        if (!name.empty()) {
            current.songName = name;
        }
    }
    return Ok(current);
}

geode::Result<LevelSettings> writeLevelSettings(LevelSettings const& settings) {
    auto* editor = LevelEditorLayer::get();
    auto* target = settingsObject();
    if (editor == nullptr || target == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }

    // Validated before anything is written, so a request with one bad field changes
    // nothing at all rather than half-applying and reporting failure.
    if (settings.background) {
        // The game's own background set is small; anything beyond it renders as nothing,
        // which looks like a broken level rather than a rejected request.
        if (auto checked = inRange("background", *settings.background, 0, 100); !checked) {
            return Err(checked.unwrapErr());
        }
    }
    if (settings.ground) {
        if (auto checked = inRange("ground", *settings.ground, 0, 100); !checked) {
            return Err(checked.unwrapErr());
        }
    }
    if (settings.groundLine) {
        if (auto checked = inRange("ground_line", *settings.groundLine, 0, 50); !checked) {
            return Err(checked.unwrapErr());
        }
    }
    if (settings.middleGround) {
        if (auto checked = inRange("middle_ground", *settings.middleGround, 0, 100); !checked) {
            return Err(checked.unwrapErr());
        }
    }
    if (settings.startMode) {
        // GameMode's enum values are small and the editor only offers the first handful.
        if (auto checked = inRange("start_mode", *settings.startMode, 0, 12); !checked) {
            return Err(checked.unwrapErr());
        }
    }

    // Recorded as the "before" values so the log can say what changed. Read *before* the
    // writes, and unwrapped immediately: `Result::operator->` on this Geode version hands
    // back the `Result` rather than the value, which reads like a pointer and is not.
    auto const beforeResult = readLevelSettings();
    if (!beforeResult) {
        return beforeResult;
    }
    auto const before = beforeResult.unwrap();

    if (settings.background) target->m_backgroundIndex = *settings.background;
    if (settings.ground) target->m_groundIndex = *settings.ground;
    if (settings.groundLine) target->m_groundLineIndex = *settings.groundLine;
    if (settings.middleGround) target->m_middleGroundIndex = *settings.middleGround;
    if (settings.startMode) target->m_startMode = *settings.startMode;
    if (settings.startMini) target->m_startMini = *settings.startMini;
    if (settings.startDual) target->m_startDual = *settings.startDual;
    if (settings.twoPlayerMode) target->m_twoPlayerMode = *settings.twoPlayerMode;
    if (settings.platformerMode) target->m_platformerMode = *settings.platformerMode;
    if (settings.reverseGameplay) target->m_reverseGameplay = *settings.reverseGameplay;
    if (settings.rotateGameplay) target->m_rotateGameplay = *settings.rotateGameplay;
    if (settings.mirrorMode) target->m_mirrorMode = *settings.mirrorMode;
    if (settings.songOffset) target->m_songOffset = *settings.songOffset;
    if (settings.audioTrack) {
        if (auto* level = target->m_level) {
            // The custom catalogue has no fixed indices to step through, so an index is
            // meaningless there. Checked rather than assumed, because writing one anyway
            // would appear to succeed and change nothing audible.
            if (level->m_songID != 0) {
                return Err(fmt::format(
                    "UNSUPPORTED_PROPERTY: this level uses song catalogue {} rather than the "
                    "normal one, so an index into it cannot be set from here",
                    level->m_songID
                ));
            }
            level->m_audioTrack = *settings.audioTrack;
        } else {
            return Err("INTERNAL_ERROR: the editor has no level to set a song on");
        }
    }

    // One call, and it is the game's: `LevelSettingsLayer` calls this when it closes, and
    // it re-creates the background, ground, ground line and middleground from the indices
    // and re-applies the colours. Reproducing that here would mean copying node positions,
    // scales and draw order that the game already gets right.
    editor->levelSettingsUpdated();

    auto const afterResult = readLevelSettings();
    if (!afterResult) {
        return afterResult;
    }
    auto const after = afterResult.unwrap();

    auto const describe = [](std::optional<int> const& value) {
        return value ? fmt::format("{}", *value) : std::string("?");
    };
    log::info(
        "level settings: bg {}->{}, ground {}->{} (line {}->{}), mg {}->{}, "
        "mode {}->{}, platformer {}->{}, mini {}->{}",
        describe(before.background), describe(after.background),
        describe(before.ground), describe(after.ground),
        describe(before.groundLine), describe(after.groundLine),
        describe(before.middleGround), describe(after.middleGround),
        describe(before.startMode), describe(after.startMode),
        static_cast<int>(before.platformerMode.value_or(false)),
        static_cast<int>(after.platformerMode.value_or(false)),
        static_cast<int>(before.startMini.value_or(false)),
        static_cast<int>(after.startMini.value_or(false))
    );

    return Ok(after);
}

matjson::Value levelSettingsToJson(LevelSettings const& settings) {
    auto result = matjson::Value::object();
    if (settings.background) result["background"] = *settings.background;
    if (settings.ground) result["ground"] = *settings.ground;
    if (settings.groundLine) result["ground_line"] = *settings.groundLine;
    if (settings.middleGround) result["middle_ground"] = *settings.middleGround;
    if (settings.startMode) result["start_mode"] = *settings.startMode;
    if (settings.startMini) result["start_mini"] = *settings.startMini;
    if (settings.startDual) result["start_dual"] = *settings.startDual;
    if (settings.twoPlayerMode) result["two_player"] = *settings.twoPlayerMode;
    if (settings.platformerMode) result["platformer"] = *settings.platformerMode;
    if (settings.reverseGameplay) result["reverse"] = *settings.reverseGameplay;
    if (settings.rotateGameplay) result["rotate_gameplay"] = *settings.rotateGameplay;
    if (settings.mirrorMode) result["mirror"] = *settings.mirrorMode;
    if (settings.songOffset) result["song_offset"] = static_cast<double>(*settings.songOffset);
    if (settings.songType) result["song_type"] = *settings.songType;
    if (settings.audioTrack) result["audio_track"] = *settings.audioTrack;
    if (settings.songName) result["song_name"] = *settings.songName;
    if (settings.startSpeed) result["start_speed"] = *settings.startSpeed;
    return result;
}

geode::Result<matjson::Value> readLevelMusic() {
    auto* settings = settingsObject();
    if (settings == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }
    auto result = matjson::Value::object();
    result["song_offset_s"] = static_cast<double>(settings->m_songOffset);
    result["song_type"] = 0;
    result["custom_song_id"] = matjson::Value(nullptr);
    result["length_ms"] = matjson::Value(nullptr);
    result["file_path"] = matjson::Value(nullptr);
    result["guidelines"] = matjson::Value(nullptr);

    auto* level = settings->m_level;
    if (level == nullptr) {
        return Ok(result);
    }
    // See `LevelSettings::songType`: `m_songID` is the catalogue, `m_audioTrack` the index.
    result["song_type"] = level->m_songID;
    result["audio_track"] = level->m_audioTrack;
    std::string const name = level->getSongName();
    if (!name.empty()) {
        result["song_name"] = name;
    }
    if (level->m_songID == 0) {
        // An official track plays from a file in the game's resources; its name comes
        // from the game's own table, and the full path from the same lookup the game
        // uses to open it.
        std::string const file = LevelTools::getAudioFileName(level->m_audioTrack);
        if (!file.empty()) {
            std::string const full =
                CCFileUtils::sharedFileUtils()->fullPathForFilename(file.c_str(), false);
            result["file_path"] = full;
            std::error_code error;
            result["file_exists"] = std::filesystem::exists(full, error);
        }
    }
    return Ok(result);
}

geode::Result<LevelSettings, std::string> parseLevelSettings(matjson::Value const& json) {
    LevelSettings parsed;

    if (!json.isObject()) {
        return Err("INVALID_REQUEST: settings must be an object");
    }

    // Plain field-by-field parsing. A table of lambdas was tried first and needed
    // `char const*` returns plus a static string to build messages, because
    // `geode::Result<>` has no default constructor and cannot be held across a loop.
    // The loop saved nothing worth that.
    //
    // Unknown keys are refused rather than ignored: a request that silently drops half
    // its fields reports success for work it did not do, which is the failure mode this
    // project keeps having to design against.
    std::size_t recognised = 0;

    auto const fieldError = [](char const* field, char const* expected) {
        return fmt::format("INVALID_REQUEST: '{}' must be a {}", field, expected);
    };

    struct IntField {
        char const* name;
        std::optional<int>* slot;
    };
    for (auto const& field : std::array {
        IntField { "background", &parsed.background },
        IntField { "ground", &parsed.ground },
        IntField { "ground_line", &parsed.groundLine },
        IntField { "middle_ground", &parsed.middleGround },
        IntField { "start_mode", &parsed.startMode },
        IntField { "audio_track", &parsed.audioTrack },
    }) {
        if (!json.contains(field.name)) {
            continue;
        }
        ++recognised;
        auto number = json[field.name].asInt();
        if (!number.ok()) {
            return Err(fieldError(field.name, "an integer"));
        }
        *field.slot = static_cast<int>(number.unwrap());
    }

    struct BoolField {
        char const* name;
        std::optional<bool>* slot;
    };
    for (auto const& field : std::array {
        BoolField { "start_mini", &parsed.startMini },
        BoolField { "start_dual", &parsed.startDual },
        BoolField { "two_player", &parsed.twoPlayerMode },
        BoolField { "platformer", &parsed.platformerMode },
        BoolField { "reverse", &parsed.reverseGameplay },
        BoolField { "rotate_gameplay", &parsed.rotateGameplay },
        BoolField { "mirror", &parsed.mirrorMode },
    }) {
        if (!json.contains(field.name)) {
            continue;
        }
        ++recognised;
        auto flag = json[field.name].asBool();
        if (!flag.ok()) {
            return Err(fieldError(field.name, "a boolean"));
        }
        *field.slot = flag.unwrap();
    }

    if (json.contains("song_offset")) {
        ++recognised;
        auto number = json["song_offset"].asDouble();
        if (!number.ok()) {
            return Err(fieldError("song_offset", "a number"));
        }
        parsed.songOffset = static_cast<float>(number.unwrap());
    }

    // Named refusals for the song-catalogue keys, so a caller that tries them is told why
    // rather than getting a generic unrecognised-key error. `song_type` is a real field of
    // the returned settings, so seeing it and assuming it is writable is a natural mistake.
    if (json.contains("song_type") || json.contains("song_id")) {
        return Err(
            "UNSUPPORTED_PROPERTY: the song catalogue cannot be changed from here. Use "
            "`audio_track` to pick a different song from the normal catalogue."
        );
    }

    if (recognised != json.size()) {
        return Err(fmt::format(
            "UNSUPPORTED_PROPERTY: {} key(s) given but only {} recognised; unknown keys "
            "are not ignored because a partly-applied request would report success",
            json.size(), recognised
        ));
    }
    if (recognised == 0) {
        return Err("INVALID_REQUEST: no settings given");
    }

    return Ok(parsed);
}

} // namespace gdai::editor
