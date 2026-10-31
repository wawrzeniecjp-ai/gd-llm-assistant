#pragma once

#include <Geode/Result.hpp>

#include <matjson.hpp>

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

class GameObject;

namespace gdai::editor {

/// A single object property, as it travels on the wire (SPEC.md 7.5).
///
/// A tagged union rather than a variant: the wire schema has a small closed set
/// of types, and naming them keeps the conversion code readable.
struct PropertyValue {
    enum class Kind { Number, Bool, Integer, IntegerList };

    Kind kind = Kind::Number;
    double number = 0.0;
    bool boolean = false;
    std::int64_t integer = 0;
    std::vector<std::int32_t> integers;

    static PropertyValue ofNumber(double value);
    static PropertyValue ofBool(bool value);
    static PropertyValue ofInteger(std::int64_t value);
    static PropertyValue ofIntegerList(std::vector<std::int32_t> values);

    /// True for both `Number` and `Integer`.
    ///
    /// Every numeric property accepts either, and the distinction between the two
    /// kinds is a **storage** detail rather than a wire requirement. It used to be
    /// enforced, with bad results: the parser classified JSON numbers as `Number`
    /// before trying `Integer`, so a model writing `z_order: 3` was told "expects an
    /// integer" -- a refusal that describes the parser's own bug as the caller's
    /// mistake. Integer-valued properties now accept either kind and round.
    [[nodiscard]] bool isNumeric() const {
        return kind == Kind::Number || kind == Kind::Integer;
    }

    /// The value as a double, for either numeric kind. Undefined for other kinds.
    [[nodiscard]] double asDouble() const {
        return kind == Kind::Integer ? static_cast<double>(integer) : number;
    }

    /// The value rounded to an integer, for either numeric kind.
    [[nodiscard]] std::int64_t asInteger() const {
        return kind == Kind::Integer ? integer : static_cast<std::int64_t>(std::llround(number));
    }

    /// Equality by value, so a no-op write can be skipped and so prior values can
    /// be compared when deciding whether a modify actually changed anything.
    bool operator==(PropertyValue const& other) const;
};

/// Property names the mod accepts, in schema order.
///
/// Unknown names are rejected with `UNSUPPORTED_PROPERTY` before any mutation
/// (SPEC.md 7.5), which is why this is a closed list rather than a passthrough.
///
/// `color_channel_1` and `color_channel_2` are the level's colour-page ids (save
/// keys 155 and 156, measured) -- the object points at a channel and the channel
/// holds the colour. They are read from and written to `GJSpriteColor::m_colorID`,
/// and a type without that slot reports `UNSUPPORTED_PROPERTY` rather than
/// accepting a write that would go nowhere.
bool isSupportedProperty(std::string_view name);

/// True for properties `modify_objects` supports as relative deltas (SPEC.md 7.5
/// restricts deltas to `x`, `y` and `rotation`).
bool isDeltaProperty(std::string_view name);

/// Reads a property's current value, or nullopt if it has none.
///
/// Main thread only. Used to capture prior values for modify-revert, so it must
/// read through the same route `writeProperty` writes through, or a
/// read-modify-revert round trip would not return the level to its prior state.
std::optional<PropertyValue> readProperty(GameObject* object, std::string_view name);

/// Writes a property. Main thread only.
///
/// The route per property is not uniform, and that is deliberate rather than
/// incidental. Scale is the one worth reading: the differential probe measured
/// that `setScaleX`/`setScale` leave the save string unchanged while writing
/// `m_scaleX` does, which was taken to mean the field is the whole story. It is
/// not. The field is what **persists**; the setter is what the **renderer** reads.
/// Writing only the field stores the change and redraws nothing, so an object
/// scaled to 0.5 looked identical until the level was reloaded -- found in a live
/// test, not by the probe, because the probe compared save strings and never
/// looked at the object.
///
/// So scale writes the field *and* calls the setter. Colour channels likewise
/// write the id and then call `updateHSVState`, for the same reason: a cached tint
/// does not notice a field write on its own.
geode::Result<> writeProperty(GameObject* object, std::string_view name, PropertyValue const& value);

/// Applies a relative change to `x`, `y` or `rotation`. Main thread only.
geode::Result<> applyDelta(GameObject* object, std::string_view name, double delta);

/// Parses a wire property object into values, rejecting unsupported keys.
///
/// Returns `UNSUPPORTED_PROPERTY` for the first unknown key, so a request naming
/// a property we do not implement is refused rather than silently partially
/// applied (SPEC.md 7.5, 12).
geode::Result<std::vector<std::pair<std::string, PropertyValue>>, std::string> parseProperties(
    matjson::Value const& json
);

/// Serialises values back to a wire property object, for `get_object_properties`.
matjson::Value propertiesToJson(
    std::vector<std::pair<std::string, PropertyValue>> const& values
);

/// The level-coordinate Y offset relative to the cocos node position.
///
/// **Measured, not derived.** The save string's Y (key `3`) and
/// `getPositionY()` differ by a constant 90 units on GD 2.2081, confirmed across
/// four placed objects and one created programmatically. Nothing in the API
/// states this relationship, so it is a constant with a probe behind it rather
/// than a formula.
///
/// Why it matters: the protocol's `y` means the level coordinate (what the save
/// string stores and what the editor shows), while `createObject` and
/// `setPosition` take the cocos position. Skipping the conversion displaces every
/// object by three grid blocks, silently.
inline constexpr double kMeasuredLevelYOffset = 90.0;

/// Level Y (wire, save-string) -> cocos Y (`createObject`, `setPosition`).
inline double levelYToCocos(double levelY) {
    return levelY - kMeasuredLevelYOffset;
}

/// Cocos Y (`getPositionY`) -> level Y (wire, save-string).
inline double cocosYToLevel(double cocosY) {
    return cocosY + kMeasuredLevelYOffset;
}

} // namespace gdai::editor
