#pragma once

#include <Geode/Result.hpp>

#include <matjson.hpp>

namespace gdai::editor {

/// A colour channel's current colour, as the level's colour page holds it.
struct ColorChannel {
    int id = 0;
    int red = 0;
    int green = 0;
    int blue = 0;
    /// True when the channel exists. A channel that has never been used has no
    /// `ColorAction`, which is why reading one back needs this flag rather than
    /// reporting black.
    bool exists = false;
};

/// Reads a colour channel (SPEC.md 7.5.2). Main thread only.
///
/// Reporting is separated from `get_object_properties` on purpose: a channel is
/// **level** state shared by every object that points at it. Two objects with
/// `color_channel_1 = 3` show the same colour, and a tool that edited colour "on an
/// object" would be describing something the game does not model.
ColorChannel readColorChannel(int channelId);

/// Sets a colour channel's colour. Main thread only.
///
/// Writes `ColorAction::m_fromColor`, then updates the action and re-tints every
/// object using the channel. All three steps are needed: the colour is stored in the
/// action, but each object caches its tint, so writing the action alone changes what
/// the level *saves* and not what it shows -- the same split that made `scale_x`
/// look broken (`properties.hpp`).
///
/// Re-tinting is per object rather than via `LevelEditorLayer::updateObjectColors`,
/// which takes a **required** `CCArray*`: passing null for "the whole level" reads
/// `array->count()` at address 0x38 and crashes the game.
///
/// Fails with `OBJECT_NOT_FOUND` when no channel with that id exists: creating one
/// would need a colour trigger to bring it into being, which is out of scope here and
/// would be a guess about the user's intent.
geode::Result<ColorChannel> writeColorChannel(int channelId, int red, int green, int blue);

/// Converts a channel to the protocol result object.
matjson::Value colorChannelToJson(ColorChannel const& channel);

/// TEMPORARY (spike): dumps everything colour-related about the current level.
///
/// **Read-only.** Calls no setters and mutates nothing, so it is safe to run on a
/// level the user cares about.
///
/// Writing a channel's colour failed three ways, each time returning the value that
/// had just been written and losing it by the next request. Two things were never
/// checked and both are decisive:
///
///  - `ColorAction::m_colorID`. If `getColorAction(n)` returns an action whose
///    `m_colorID` is not `n`, the lookup is not the id-keyed mapping it looks like,
///    and every write so far has been aimed at an arbitrary action.
///  - `LevelEditorLayer::m_colorTriggers`. The editor's own colour popup is built
///    from **both** a `ColorAction` and an `EffectGameObject`, which suggests the
///    durable store is the trigger object and `ColorAction` is derived state rebuilt
///    from it -- which is exactly the shape that would explain a write surviving in
///    memory and vanishing on the next read.
///
/// Must be called on the main thread.
void runColorProbe();

} // namespace gdai::editor
