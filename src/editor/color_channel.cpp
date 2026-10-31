#include "color_channel.hpp"

#include <Geode/binding/ColorAction.hpp>
#include <Geode/binding/EffectGameObject.hpp>
#include <Geode/binding/GJEffectManager.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/binding/LevelSettingsObject.hpp>

#include <algorithm>
#include <charconv>

#include "transaction.hpp"

using namespace geode::prelude;

namespace gdai::editor {
namespace {

/// The level's effect manager, which owns the colour channels.
///
/// `m_effectManager` is on `GJBaseGameLayer`, so the editor's own inherited member is
/// the one to use -- the editor *is* a base game layer while it is open.
GJEffectManager* effectManager() {
    auto* editor = LevelEditorLayer::get();
    return editor == nullptr ? nullptr : editor->m_effectManager;
}

int clampedByte(int value) {
    return std::clamp(value, 0, 255);
}

ColorChannel describe(ColorAction* action, int channelId) {
    ColorChannel channel;
    channel.id = channelId;
    if (action == nullptr) {
        return channel;
    }
    channel.exists = true;
    // `m_color` is the **live** colour; `m_fromColor` is not.
    //
    // Measured by probe: channel 1 held `from=(255,255,255)` while
    // `color=(0,18,255)`, and the level on screen showed the blue. Reading
    // `m_fromColor` therefore reported every channel as white -- which is what made
    // three correct-looking writes look like no-ops, and made the user's own manual
    // colour edits invisible to the model.
    channel.red = action->m_color.r;
    channel.green = action->m_color.g;
    channel.blue = action->m_color.b;
    return channel;
}

/// The stored `kS38` entry for one channel, or empty when the level has none.
///
/// Reads the level's own save string rather than any object's state. Measured:
/// writing through the setup format changed the entry here while `ColorAction`
/// still reported the old colour, so this is the store and that is derived.
std::string savedEntryFor(int channelId) {
    auto* editor = LevelEditorLayer::get();
    auto* settings = editor == nullptr ? nullptr : editor->m_levelSettings;
    if (settings == nullptr) {
        return {};
    }
    auto const setup = settings->getSaveString();
    auto const at = setup.find("kS38,");
    if (at == std::string::npos) {
        return {};
    }
    auto const body = setup.substr(at + 5);
    auto const stop = body.find(",k");
    auto const colours = stop == std::string::npos ? body : body.substr(0, stop);
    auto const needle = fmt::format("_6_{}_", channelId);
    auto const pos = colours.find(needle);
    if (pos == std::string::npos) {
        return {};
    }
    auto const startAt = colours.rfind('|', pos);
    auto const begin = startAt == std::string::npos ? 0 : startAt + 1;
    auto const endAt = colours.find('|', pos);
    return colours.substr(
        begin, endAt == std::string::npos ? std::string::npos : endAt - begin
    );
}

} // namespace

ColorChannel readColorChannel(int channelId) {
    auto* manager = effectManager();
    if (manager == nullptr) {
        return ColorChannel { .id = channelId };
    }
    auto* action = manager->getColorAction(channelId);
    auto const channel = describe(action, channelId);
    // Temporary: pointer identity is the question being answered. If the pointer
    // changes between a write and the next read, the manager is recreating actions and
    // writing into one of them is pointless; if it is stable, the fields are the
    // problem instead.
    log::info(
        "colour read {}: action={} from=({},{},{}) color=({},{},{}) exists={}",
        channelId, static_cast<void*>(action),
        action ? action->m_fromColor.r : -1, action ? action->m_fromColor.g : -1,
        action ? action->m_fromColor.b : -1,
        action ? action->m_color.r : -1, action ? action->m_color.g : -1,
        action ? action->m_color.b : -1, channel.exists
    );
    return channel;
}

namespace {

/// Reads a `k_v` value out of one `kS38` entry, or -1 when absent.
///
/// The entry format is the level's own, measured from
/// `LevelSettingsObject::getSaveString()`:
///
///     1_<r>_2_<g>_3_<b>_11_<..>_12_<..>_13_<..>_4_<player>_6_<channel>_5_<blend>_...
///
/// So key 1 is red, 2 green, 3 blue and **6 is the channel id**. System channels are
/// not a separate namespace: BG is 1000, ground 1001, ground line 1002, middleground
/// 1004+, alongside the custom channels 1..999.
int valueForKey(std::string_view entry, int key) {
    auto const prefix = fmt::format("{}_{{", key);
    auto at = entry.find(prefix);
    // Anchored at either the start or just after a separator, so searching for
    // key `6` cannot match the tail of `16`.
    while (at != std::string_view::npos) {
        if (at == 0 || entry[at - 1] == '_') {
            break;
        }
        at = entry.find(prefix, at + 1);
    }
    if (at == std::string_view::npos) {
        return -1;
    }
    auto const digitsAt = at + prefix.size();
    auto const endAt = entry.find('_', digitsAt);
    auto const digits = entry.substr(
        digitsAt,
        endAt == std::string_view::npos ? std::string_view::npos : endAt - digitsAt
    );
    auto parsed = 0;
    auto const [ptr, ec] = std::from_chars(digits.data(), digits.data() + digits.size(), parsed);
    if (ec != std::errc() || ptr != digits.data() + digits.size()) {
        return -1;
    }
    return parsed;
}

/// Replaces a `k_v` value inside one entry, appending the pair when absent.
std::string withValue(std::string const& entry, int key, int value) {
    auto const prefix = fmt::format("{}_{{", key);
    auto at = entry.find(prefix);
    while (at != std::string::npos && at != 0 && entry[at - 1] != '_') {
        at = entry.find(prefix, at + 1);
    }
    auto const replacement = fmt::format("{}_{}_", key, value);
    if (at == std::string::npos) {
        // Appended rather than ignored: the game's parser tolerates unknown keys and
        // our absence of one must not silently drop the edit.
        return entry + replacement;
    }
    auto const digitsAt = at + prefix.size();
    auto const endAt = entry.find('_', digitsAt);
    auto const after = endAt == std::string::npos ? std::string() : entry.substr(endAt + 1);
    return entry.substr(0, at) + replacement + after;
}

} // namespace

geode::Result<ColorChannel> writeColorChannel(int channelId, int red, int green, int blue) {
    auto* manager = effectManager();
    if (manager == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }
    if (channelId <= 0) {
        return Err("INVALID_REQUEST: colour channel ids start at 1");
    }

    // Two managers, and this is the distinction that took four attempts to find.
    //
    // `LevelEditorLayer::m_effectManager` is the **live** one: it drives rendering and is
    // rebuilt from elsewhere each frame, which is why every write into it was undone
    // within a frame. `LevelSettingsObject::m_effectManager` is the level's own copy, and
    // it is what `LevelSettingsObject::getSaveString()` serialises into the `kS38`
    // section. The existence of `LevelEditorLayer::transferDefaultColors(src, dest)`
    // confirms there are two: colours are copied between them on a load.
    //
    // Logged proof: writing through the live manager left the stored entry byte-identical
    // before and after the call, so the write never reached the store at all.
    auto* editor = LevelEditorLayer::get();
    auto* settings = editor == nullptr ? nullptr : editor->m_levelSettings;
    auto* store = settings == nullptr ? nullptr : settings->m_effectManager;

    auto* action = manager->getColorAction(channelId);
    if (action == nullptr) {
        return Err(fmt::format("OBJECT_NOT_FOUND: colour channel {}", channelId));
    }
    auto* stored = store == nullptr ? nullptr : store->getColorAction(channelId);
    log::debug(
        "colour channel {}: live action={} stored action={} distinct={}",
        channelId, static_cast<void*>(action), static_cast<void*>(stored),
        action != stored
    );

    // Build the channel's setup entry from the shape the level itself uses, with our
    // three values substituted. `setupFromString` is the game's own parser for this
    // format, so the result cannot disagree with the game about which key is which.
    //
    // The alternative -- writing `m_color` -- cannot work for a structural reason:
    // `ColorAction` is an **animation** (`step(dt)`, `m_deltaTime`, `m_duration`,
    // `m_fromColor` -> `m_toColor`), so `m_color` is an interpolated frame result that
    // `step` recomputes. Writing the output of an animation is why four attempts each
    // read back correctly and were gone one frame later.
    auto entry = fmt::format("1_{}_2_{}_3_{}_6_{}_5_0_7_1_15_1_18_0_8_1",
        clampedByte(red), clampedByte(green), clampedByte(blue), channelId);

    auto const before = describe(action, channelId);
    TransactionManager::get().recordColorChange(channelId, before.red, before.green, before.blue);

    // The stored entry as it stands *before* the write, for comparison with the one
    // logged after it. Two readings a request apart looked like a delayed write;
    // logging both sides of one request says whether the value landed now or at all.
    auto const entryBefore = savedEntryFor(channelId);

    // The store first -- it is the one whose contents survive and get serialised.
    if (stored != nullptr) {
        stored->setupFromString(entry);
    } else {
        log::warn("colour channel {}: no stored action; writing live only", channelId);
    }
    action->setupFromString(entry);

    // Push the store into the live manager, which is the game's own path for exactly
    // this (`transferDefaultColors` is what a level load uses). Doing it this way means
    // the live colour comes from the store rather than being assembed here by hand.
    if (store != nullptr && editor != nullptr) {
        editor->transferDefaultColors(store, manager);
    }

    // Writing the store is only half of it, and the probe proved the two halves are
    // separate: the `kS38` entry did change (`1_255_2_0_3_0` -> `1_0_2_0_3_0`), while
    // the next `getColorAction` still reported the old colour. So the durable data
    // accepts the write and the *live* colour state is built from something else.
    //
    // These two calls are the game's own recalculation path -- `calculateBaseActiveColors`
    // rebuilds the active colours from the stored actions, `processColors` applies them.
    // Calling them here rather than writing `m_color` is the difference between
    // refreshing derived state and corrupting it: the value comes from the store either
    // way, it is just no longer stale.
    manager->calculateBaseActiveColors();
    manager->processColors();
    manager->updateColorAction(action);

    // Re-tint only the objects that use the channel. Not `updateObjectColors`: that
    // takes a required `CCArray*` and crashes on null.
    if (auto* editor = LevelEditorLayer::get()) {
        if (auto* objects = editor->getAllObjects()) {
            auto const total = static_cast<int>(objects->count());
            auto retinted = 0;
            for (int i = 0; i < total; ++i) {
                auto* object = static_cast<GameObject*>(objects->objectAtIndex(i));
                if (object == nullptr) {
                    continue;
                }
                auto const usesChannel = [channelId](GJSpriteColor* slot) {
                    return slot != nullptr && slot->m_colorID == channelId;
                };
                if (!usesChannel(object->m_baseColor) && !usesChannel(object->m_detailColor)) {
                    continue;
                }
                editor->manualUpdateObjectColors(object);
                ++retinted;
            }
            log::info("colour channel {}: {} object(s) re-tinted", channelId, retinted);
        }
        // Whether the write reached the store, which is the only claim that matters:
        // the live manager accepted four earlier versions of this and lost them all.
        auto const entryAfter = savedEntryFor(channelId);
        log::info(
            "colour channel {} stored: '{}' -> '{}'",
            channelId, entryBefore, entryAfter
        );
    }

    auto const written = describe(manager->getColorAction(channelId), channelId);
    log::info(
        "colour channel {} set to ({}, {}, {}); reads back as ({}, {}, {})",
        channelId, clampedByte(red), clampedByte(green), clampedByte(blue),
        written.red, written.green, written.blue
    );
    return Ok(written);
}

matjson::Value colorChannelToJson(ColorChannel const& channel) {
    auto result = matjson::Value::object();
    result["channel"] = channel.id;
    result["exists"] = channel.exists;
    result["red"] = channel.red;
    result["green"] = channel.green;
    result["blue"] = channel.blue;
    return result;
}

void runColorProbe() {
    auto* editor = LevelEditorLayer::get();
    auto* manager = effectManager();
    if (editor == nullptr || manager == nullptr) {
        log::warn("COLOUR-PROBE | no editor or no effect manager");
        return;
    }

    log::info("COLOUR-PROBE ===== begin =====");

    // 1. What the manager actually holds, and under which ids.
    //
    // This is the question the failed writes never answered: each `ColorAction`
    // carries its own `m_colorID`, so comparing that with the vector position says
    // whether the id space is the one being assumed.
    log::info(
        "COLOUR-PROBE | colorActionVector has {} entries, colorActionSpriteVector has {}",
        manager->m_colorActionVector.size(), manager->m_colorActionSpriteVector.size()
    );
    auto const toShow = std::min<std::size_t>(manager->m_colorActionVector.size(), 20);
    for (std::size_t i = 0; i < toShow; ++i) {
        auto* action = manager->m_colorActionVector[i];
        if (action == nullptr) {
            log::info("COLOUR-PROBE | vector[{}] = null", i);
            continue;
        }
        log::info(
            "COLOUR-PROBE | vector[{}] ptr={} m_colorID={} from=({},{},{}) to=({},{},{}) "
            "color=({},{},{})",
            i, static_cast<void*>(action), action->m_colorID,
            action->m_fromColor.r, action->m_fromColor.g, action->m_fromColor.b,
            action->m_toColor.r, action->m_toColor.g, action->m_toColor.b,
            action->m_color.r, action->m_color.g, action->m_color.b
        );
    }

    // 2. Does `getColorAction(n)` return the action whose `m_colorID` is n?
    //
    // A "no" here is the whole bug: it means the id being written to is not the id
    // being asked for, which is consistent with a write that appears to take and is
    // gone by the next request.
    for (int channel = 1; channel <= 6; ++channel) {
        auto* action = manager->getColorAction(channel);
        if (action == nullptr) {
            log::info("COLOUR-PROBE | getColorAction({}) = null", channel);
            continue;
        }
        auto const matches = action->m_colorID == channel;
        log::info(
            "COLOUR-PROBE | getColorAction({}) ptr={} m_colorID={} {} from=({},{},{})",
            channel, static_cast<void*>(action), action->m_colorID,
            matches ? "MATCH" : "MISMATCH",
            action->m_fromColor.r, action->m_fromColor.g, action->m_fromColor.b
        );
    }

    // 3. The durable store, if there is one: the editor's colour triggers.
    auto* triggers = editor->m_colorTriggers;
    auto const triggerCount = triggers == nullptr ? 0 : static_cast<int>(triggers->count());
    log::info("COLOUR-PROBE | m_colorTriggers has {} entries", triggerCount);
    for (int i = 0; i < std::min(triggerCount, 20); ++i) {
        auto* object = static_cast<GameObject*>(triggers->objectAtIndex(i));
        if (object == nullptr) {
            log::info("COLOUR-PROBE | trigger[{}] = null", i);
            continue;
        }
        auto* effect = typeinfo_cast<EffectGameObject*>(object);
        auto const saveString = object->getSaveString(editor);
        log::info(
            "COLOUR-PROBE | trigger[{}] ptr={} objectID={} isEffect={} m_targetColor={}",
            i, static_cast<void*>(object), object->m_objectID, effect != nullptr,
            object->m_targetColor
        );
        if (effect != nullptr) {
            log::info(
                "COLOUR-PROBE | trigger[{}] m_targetGroupID={} triggerTargetColor=({},{},{}) "
                "hsv=({},{},{}) legacyHSV={}",
                i, effect->m_targetGroupID,
                effect->m_triggerTargetColor.r, effect->m_triggerTargetColor.g,
                effect->m_triggerTargetColor.b,
                effect->m_hsvValue.h, effect->m_hsvValue.s, effect->m_hsvValue.v,
                effect->m_legacyHSV
            );
        }
        log::info(
            "COLOUR-PROBE | trigger[{}] save='{}'",
            i, saveString.size() > 220 ? saveString.substr(0, 220) + "..." : saveString
        );
    }

    // 4. The system channels, which the user asked about specifically.
    //
    // BG / G / G2 / LINE / MG are not colour *channels* in the 1-999 sense: they are
    // level-level colours paired with a background **pattern index**, which is why
    // "set BG to X" is two values and not one. Reported here so the shape is on
    // record before anything is built on top of it.
    if (auto* settings = editor->m_levelSettings) {
        log::info(
            "COLOUR-PROBE | levelSettings backgroundIndex={} groundIndex={} "
            "middleGroundIndex={} groundLineIndex={} colorPage={} platformer={}",
            settings->m_backgroundIndex, settings->m_groundIndex,
            settings->m_middleGroundIndex, settings->m_groundLineIndex,
            settings->m_colorPage, settings->m_platformerMode
        );
    }

    if (auto* background = editor->m_background) {
        log::info(
            "COLOUR-PROBE | m_background present; ground layers {} / {}",
            static_cast<void*>(editor->m_groundLayer),
            static_cast<void*>(editor->m_groundLayer2)
        );
        (void) background;
    }

    // 5. The authoritative store: the colour setup string.
    //
    // This is the decisive one. `ColorAction` is an *animation* -- it has `step(dt)`,
    // `m_deltaTime`, `m_duration` and `m_fromColor` -> `m_toColor`, so `m_color` is an
    // interpolated frame result rather than a stored value. Something must own the
    // real colour, and `GJEffectManager` has both `getSaveString()` and
    // `setupFromString()`, which is the shape of a serialised store: parse in, write
    // out. A level's colours travel in the level string's colour section, so if this
    // string holds the channel colours, the round trip is the way to write them --
    // using the game's own parser instead of guessing which field it reads.
    //
    // Read-only: it is dumped, not modified.
    if (auto* manager = effectManager()) {
        auto const setup = manager->getSaveString();
        log::info("COLOUR-PROBE | manager getSaveString len={}", setup.size());
        log::info("COLOUR-PROBE | manager setup='{}'", setup);
    }
    if (auto* settings = editor->m_levelSettings) {
        auto const levelSettings = settings->getSaveString();
        log::info(
            "COLOUR-PROBE | levelSettings getSaveString len={}", levelSettings.size()
        );
        log::info("COLOUR-PROBE | levelSettings setup='{}'", levelSettings);
    }

    log::info("COLOUR-PROBE ===== end =====");
}

} // namespace gdai::editor
