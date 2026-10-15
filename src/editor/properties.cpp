#include "properties.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/GJSpriteColor.hpp>
#include <Geode/binding/GameObject.hpp>

#include <algorithm>
#include <array>

using namespace geode::prelude;

namespace gdai::editor {
namespace {

/// z-layer names to the game's enum values (SPEC.md 7.5).
///
/// Read from `ZLayer` in the generated Enums.hpp rather than assumed: the values
/// are **not** contiguous and not sequential, so a guessed mapping would land
/// objects on the wrong layer with no error. The differential probe confirmed
/// that the save string stores this raw value (writing 9 produced key 24 = 9).
struct ZLayerName {
    std::string_view name;
    ZLayer value;
};

constexpr std::array<ZLayerName, 11> kZLayers {{
    { "b5", ZLayer::B5 },
    { "b4", ZLayer::B4 },
    { "b3", ZLayer::B3 },
    { "b2", ZLayer::B2 },
    { "b1", ZLayer::B1 },
    { "default", ZLayer::Default },
    { "portal_back", ZLayer::PortalBack },
    { "t1", ZLayer::T1 },
    { "t2", ZLayer::T2 },
    { "t3", ZLayer::T3 },
    { "t4", ZLayer::T4 },
}};

std::optional<ZLayer> parseZLayer(std::string_view name) {
    auto const found = std::find_if(kZLayers.begin(), kZLayers.end(),
        [name](auto const& entry) { return entry.name == name; });
    if (found == kZLayers.end()) {
        return std::nullopt;
    }
    return found->value;
}

std::string_view zLayerName(ZLayer value) {
    auto const found = std::find_if(kZLayers.begin(), kZLayers.end(),
        [value](auto const& entry) { return entry.value == value; });
    return found == kZLayers.end() ? std::string_view("unknown") : found->name;
}

/// Groups live in a fixed array of 10 shorts, so both the capacity and the value
/// range are bounded by the game rather than by us (SPEC.md 7.5 caps the wire at
/// 10 to match).
std::vector<std::int32_t> readGroups(GameObject* object) {
    std::vector<std::int32_t> groups;
    if (object->m_groups == nullptr) {
        return groups;
    }
    for (auto group : *object->m_groups) {
        if (group > 0) {
            groups.push_back(group);
        }
    }
    return groups;
}

void writeGroups(GameObject* object, std::vector<std::int32_t> const& groups) {
    // Remove groups no longer wanted, then add the rest. `addToGroup` is
    // idempotent, so re-adding an existing group is harmless.
    for (auto const existing : readGroups(object)) {
        if (std::find(groups.begin(), groups.end(), existing) == groups.end()) {
            object->removeFromGroup(static_cast<int>(existing));
        }
    }
    for (auto const group : groups) {
        object->addToGroup(static_cast<int>(group));
    }
}

} // namespace

PropertyValue PropertyValue::ofNumber(double value) {
    PropertyValue result;
    result.kind = Kind::Number;
    result.number = value;
    return result;
}

PropertyValue PropertyValue::ofBool(bool value) {
    PropertyValue result;
    result.kind = Kind::Bool;
    result.boolean = value;
    return result;
}

PropertyValue PropertyValue::ofInteger(std::int64_t value) {
    PropertyValue result;
    result.kind = Kind::Integer;
    result.integer = value;
    return result;
}

PropertyValue PropertyValue::ofIntegerList(std::vector<std::int32_t> values) {
    PropertyValue result;
    result.kind = Kind::IntegerList;
    result.integers = std::move(values);
    return result;
}

bool PropertyValue::operator==(PropertyValue const& other) const {
    if (kind != other.kind) {
        return false;
    }
    switch (kind) {
        case Kind::Number: return number == other.number;
        case Kind::Bool: return boolean == other.boolean;
        case Kind::Integer: return integer == other.integer;
        case Kind::IntegerList: return integers == other.integers;
    }
    return false;
}

namespace {

/// Every property the mod accepts, with the facts each read/write needs.
struct PropertySpec {
    std::string_view name;
    bool deltaCapable;
};

constexpr std::array<PropertySpec, 11> kSpecs {{
    { "x", true },
    { "y", true },
    { "rotation", true },
    { "scale_x", false },
    { "scale_y", false },
    { "flip_x", false },
    { "flip_y", false },
    { "z_layer", false },
    { "z_order", false },
    { "editor_layer", false },
    { "groups", false },
}};

} // namespace

bool isSupportedProperty(std::string_view name) {
    return std::any_of(kSpecs.begin(), kSpecs.end(),
        [name](auto const& spec) { return spec.name == name; });
}

bool isDeltaProperty(std::string_view name) {
    auto const found = std::find_if(kSpecs.begin(), kSpecs.end(),
        [name](auto const& spec) { return spec.name == name; });
    return found != kSpecs.end() && found->deltaCapable;
}

std::optional<PropertyValue> readProperty(GameObject* object, std::string_view name) {
    if (object == nullptr) {
        return std::nullopt;
    }

    // Reads mirror writes exactly. In particular y converts back from the cocos
    // position, so read -> modify -> revert is a true round trip.
    if (name == "x") return PropertyValue::ofNumber(object->getPositionX());
    if (name == "y") return PropertyValue::ofNumber(cocosYToLevel(object->getPositionY()));
    if (name == "rotation") return PropertyValue::ofNumber(object->getRotation());
    if (name == "scale_x") return PropertyValue::ofNumber(object->m_scaleX);
    if (name == "scale_y") return PropertyValue::ofNumber(object->m_scaleY);
    if (name == "flip_x") return PropertyValue::ofBool(object->m_isFlipX);
    if (name == "flip_y") return PropertyValue::ofBool(object->m_isFlipY);
    if (name == "z_layer") return PropertyValue::ofInteger(static_cast<std::int64_t>(object->m_zLayer));
    if (name == "z_order") return PropertyValue::ofInteger(object->m_zOrder);
    if (name == "editor_layer") return PropertyValue::ofInteger(object->m_editorLayer);
    if (name == "groups") return PropertyValue::ofIntegerList(readGroups(object));

    return std::nullopt;
}

geode::Result<> writeProperty(GameObject* object, std::string_view name, PropertyValue const& value) {
    if (object == nullptr) {
        return Err("OBJECT_NOT_FOUND: no object to modify");
    }
    if (!isSupportedProperty(name)) {
        return Err(fmt::format("UNSUPPORTED_PROPERTY: '{}'", name));
    }

    // Position is written as a pair: setting x and y separately would need two
    // calls, and reading one back mid-way can pick up a stale component.
    if (name == "x" || name == "y") {
        if (value.kind != PropertyValue::Kind::Number) {
            return Err(fmt::format("INVALID_REQUEST: '{}' expects a number", name));
        }
        auto x = object->getPositionX();
        auto levelY = cocosYToLevel(object->getPositionY());
        if (name == "x") {
            x = value.number;
        } else {
            levelY = value.number;
        }
        object->setPosition({ static_cast<float>(x), static_cast<float>(levelYToCocos(levelY)) });
        return Ok();
    }

    if (name == "rotation") {
        if (value.kind != PropertyValue::Kind::Number) {
            return Err("INVALID_REQUEST: 'rotation' expects a number");
        }
        object->setRotation(static_cast<float>(value.number));
        return Ok();
    }

    if (name == "scale_x" || name == "scale_y") {
        if (value.kind != PropertyValue::Kind::Number || value.number <= 0.0) {
            return Err(fmt::format("INVALID_REQUEST: '{}' expects a positive number", name));
        }
        // Fields, not setters: measured that `setScaleX`/`setScale` leave the
        // save string unchanged while the field write does not.
        if (name == "scale_x") {
            object->m_scaleX = static_cast<float>(value.number);
        } else {
            object->m_scaleY = static_cast<float>(value.number);
        }
        return Ok();
    }

    if (name == "flip_x" || name == "flip_y") {
        if (value.kind != PropertyValue::Kind::Bool) {
            return Err(fmt::format("INVALID_REQUEST: '{}' expects a boolean", name));
        }
        if (name == "flip_x") {
            object->setFlipX(value.boolean);
        } else {
            object->setFlipY(value.boolean);
        }
        return Ok();
    }

    if (name == "z_layer") {
        // Accepts either the name or the raw enum value, so a caller holding a
        // value read back from readProperty can write it without a lookup.
        if (value.kind == PropertyValue::Kind::Integer) {
            object->m_zLayer = static_cast<ZLayer>(value.integer);
            return Ok();
        }
        return Err("INVALID_REQUEST: 'z_layer' expects a name or an integer");
    }

    if (name == "z_order") {
        if (value.kind != PropertyValue::Kind::Integer) {
            return Err("INVALID_REQUEST: 'z_order' expects an integer");
        }
        object->m_zOrder = static_cast<int>(value.integer);
        return Ok();
    }

    if (name == "editor_layer") {
        if (value.kind != PropertyValue::Kind::Integer) {
            return Err("INVALID_REQUEST: 'editor_layer' expects an integer");
        }
        object->m_editorLayer = static_cast<short>(value.integer);
        return Ok();
    }

    if (name == "groups") {
        if (value.kind != PropertyValue::Kind::IntegerList) {
            return Err("INVALID_REQUEST: 'groups' expects a list of integers");
        }
        writeGroups(object, value.integers);
        return Ok();
    }

    return Err(fmt::format("UNSUPPORTED_PROPERTY: '{}'", name));
}

geode::Result<> applyDelta(GameObject* object, std::string_view name, double delta) {
    if (!isDeltaProperty(name)) {
        return Err(fmt::format("INVALID_REQUEST: '{}' does not support a delta", name));
    }
    auto current = readProperty(object, name);
    if (!current.has_value()) {
        return Err(fmt::format("OBJECT_NOT_FOUND: could not read '{}'", name));
    }
    return writeProperty(object, name, PropertyValue::ofNumber(current->number + delta));
}

geode::Result<std::vector<std::pair<std::string, PropertyValue>>, std::string> parseProperties(
    matjson::Value const& json
) {
    std::vector<std::pair<std::string, PropertyValue>> parsed;

    if (!json.isObject()) {
        return Err("INVALID_REQUEST: properties must be an object");
    }

    // matjson does not expose object key iteration, so this walks the known
    // property list instead of the input. That inverts the usual loop, so the
    // unknown-key check is done by counting: if the object holds more keys than
    // were recognised, something unrecognised is present. Rejecting the request
    // whole matters because silently ignoring a key would report success for a
    // request that was not fully applied (SPEC.md 7.5, 12).
    std::size_t recognised = 0;

    for (auto const& spec : kSpecs) {
        auto const name = std::string(spec.name);
        if (!json.contains(name)) {
            continue;
        }
        ++recognised;
        auto const item = json[name];

        if (name == "groups") {
            if (!item.isArray()) {
                return Err("INVALID_REQUEST: 'groups' must be an array");
            }
            std::vector<std::int32_t> groups;
            for (auto const& entry : item) {
                auto number = entry.asInt();
                if (!number.ok()) {
                    return Err("INVALID_REQUEST: 'groups' must contain integers");
                }
                groups.push_back(static_cast<std::int32_t>(number.unwrap()));
            }
            parsed.emplace_back(name, PropertyValue::ofIntegerList(std::move(groups)));
            continue;
        }

        if (name == "flip_x" || name == "flip_y") {
            auto boolean = item.asBool();
            if (!boolean.ok()) {
                return Err(fmt::format("INVALID_REQUEST: '{}' must be a boolean", name));
            }
            parsed.emplace_back(name, PropertyValue::ofBool(boolean.unwrap()));
            continue;
        }

        if (name == "z_layer") {
            // A name is preferred on the wire; the raw integer is also accepted
            // so a value read by readProperty can be written back unchanged.
            if (auto text = item.asString(); text.ok()) {
                auto const layer = parseZLayer(text.unwrap());
                if (!layer.has_value()) {
                    return Err(fmt::format("INVALID_REQUEST: unknown z-layer '{}'", text.unwrap()));
                }
                parsed.emplace_back(name, PropertyValue::ofInteger(static_cast<std::int64_t>(*layer)));
                continue;
            }
        }

        if (auto number = item.asDouble(); number.ok()) {
            parsed.emplace_back(name, PropertyValue::ofNumber(number.unwrap()));
            continue;
        }
        if (auto integer = item.asInt(); integer.ok()) {
            parsed.emplace_back(name, PropertyValue::ofInteger(integer.unwrap()));
            continue;
        }

        return Err(fmt::format("INVALID_REQUEST: unsupported value for '{}'", name));
    }

    if (json.size() != recognised) {
        return Err("UNSUPPORTED_PROPERTY: the object contains keys this mod does not support");
    }

    return Ok(std::move(parsed));
}

matjson::Value propertiesToJson(
    std::vector<std::pair<std::string, PropertyValue>> const& values
) {
    auto result = matjson::Value::object();
    for (auto const& [name, value] : values) {
        switch (value.kind) {
            case PropertyValue::Kind::Number:
                result[name] = value.number;
                break;
            case PropertyValue::Kind::Bool:
                result[name] = value.boolean;
                break;
            case PropertyValue::Kind::Integer:
                // z_layer travels as its name when it is one, so the wire stays
                // readable and matches the schema's enum.
                if (name == "z_layer") {
                    result[name] = std::string(zLayerName(static_cast<ZLayer>(value.integer)));
                } else {
                    result[name] = value.integer;
                }
                break;
            case PropertyValue::Kind::IntegerList: {
                auto array = matjson::Value::array();
                for (auto const entry : value.integers) {
                    array.push(entry);
                }
                result[name] = std::move(array);
                break;
            }
        }
    }
    return result;
}

} // namespace gdai::editor
