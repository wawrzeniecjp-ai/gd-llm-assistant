#include "color_channel.hpp"

#include <Geode/binding/ColorAction.hpp>
#include <Geode/binding/EffectGameObject.hpp>
#include <Geode/binding/GJEffectManager.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/binding/LevelSettingsObject.hpp>

#include <algorithm>

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

geode::Result<ColorChannel> writeColorChannel(int channelId, int red, int green, int blue) {
    auto* manager = effectManager();
    if (manager == nullptr) {
        return Err("EDITOR_NOT_OPEN: no editor layer is active");
    }

    // Refused, because writing here does not persist -- measured, not assumed.
    //
    // Writing `GJEffectManager`'s colour state was tried four ways: the raw
    // `m_fromColor`/`m_toColor`/`m_color` fields, `ColorAction::updateCustomColor`,
    // the `ColorActionSprite` copy that objects are actually tinted from, and each of
    // those followed by `updateColorAction` and a per-object re-tint. Every one
    // reported the new value back immediately and every one was undone **within one
    // frame**:
    //
    //     set to (0,0,0)  ->  reads back as (0,0,0)
    //     next frame:     from=(255,255,255) color=(255,0,0)   <- the original
    //
    // So `GJEffectManager` is derived state, rebuilt each frame from an authoritative
    // store, and nothing written into it survives. That store is the **colour trigger
    // objects** -- visible in the editor's own design, where `ColorSelectPopup` is
    // constructed from both an `EffectGameObject` and a `ColorAction`.
    //
    // Implementing it properly means creating or updating a colour trigger object
    // (type 899) whose target channel is this one, inside the turn's transaction so
    // Revert removes it. That is a real feature with a real design cost -- it adds an
    // object to the level -- and it is not something to bolt on by guessing at the
    // trigger's save-string keys.
    //
    // Refused rather than left returning success: a tool that reports a change the
    // level does not keep is the failure that wastes the most of a user's time, because
    // they have to verify every claim it makes. Reading works and is left available, so
    // the model can still describe colours and name the channel to change.
    auto const current = describe(manager->getColorAction(channelId), channelId);
    log::warn(
        "colour channel {}: write refused; current=({},{},{}) requested=({},{},{})",
        channelId, current.red, current.green, current.blue,
        clampedByte(red), clampedByte(green), clampedByte(blue)
    );
    return Err(fmt::format(
        "UNSUPPORTED_PROPERTY: this build cannot set colours. The level rebuilds its "
        "colour state every frame from its colour triggers, so nothing written here "
        "survives. Channel {} is currently ({}, {}, {}). Tell the user to change it on "
        "the level's colour page.",
        channelId, current.red, current.green, current.blue
    ));
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

    log::info("COLOUR-PROBE ===== end =====");
}

} // namespace gdai::editor
