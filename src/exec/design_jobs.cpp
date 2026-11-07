#include "design_jobs.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>

#include <fmt/format.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "../editor/level_revision.hpp"
#include "../editor/object_catalog.hpp"
#include "../editor/properties.hpp"

using namespace geode::prelude;

namespace gdai::exec {
namespace {

/// The named properties reported per object; the same list `get_object_properties`
/// reports (SPEC.md 7.5).
constexpr std::array<std::string_view, 13> kRecordProperties {{
    "x", "y", "rotation", "scale_x", "scale_y", "flip_x", "flip_y",
    "z_layer", "z_order", "editor_layer", "groups",
    "color_channel_1", "color_channel_2",
}};

constexpr int kDefaultLevelPage = 1000;
constexpr int kMaxLevelPage = 2000;
constexpr int kDefaultDescribePage = 200;
constexpr int kMaxDescribePage = 500;

/// Ids that must not be instantiated, even off-scene.
///
/// Empty until spike S-L2 runs the full dump; any id that crashes the game goes
/// here, with a note of what happened. The dump logs each id *before* creating it
/// so that the last line of a crash log names the culprit.
constexpr std::array<int, 0> kDescribeDenylist{};

bool isDenylisted(int id) {
    return std::find(kDescribeDenylist.begin(), kDescribeDenylist.end(), id)
        != kDescribeDenylist.end();
}

/// A request refused during validation.
class FailedJob final : public Job {
public:
    FailedJob(std::string code, std::string message)
        : m_code(std::move(code)), m_message(std::move(message)) {}

    bool step() override { return true; }
    JobOutcome takeOutcome() override { return JobOutcome::failure(m_code, m_message); }
    std::string_view type() const override { return "error"; }

private:
    std::string m_code;
    std::string m_message;
};

std::unique_ptr<Job> invalid(std::string message) {
    return std::make_unique<FailedJob>("INVALID_REQUEST", std::move(message));
}

/// `<offset>@<revision>`. Opaque to the caller; carries the revision so a cursor
/// from before an edit is refused rather than silently skipping or repeating
/// objects.
std::string encodeCursor(int offset, std::int64_t revision) {
    return fmt::format("{}@{}", offset, revision);
}

struct Cursor {
    int offset = 0;
    std::int64_t revision = 0;
};

std::optional<Cursor> decodeCursor(std::string const& text) {
    auto const at = text.find('@');
    if (at == std::string::npos) {
        return std::nullopt;
    }
    Cursor cursor;
    auto const* begin = text.data();
    auto const* end = text.data() + text.size();
    auto first = std::from_chars(begin, begin + at, cursor.offset);
    auto second = std::from_chars(begin + at + 1, end, cursor.revision);
    if (first.ec != std::errc{} || first.ptr != begin + at
        || second.ec != std::errc{} || second.ptr != end || cursor.offset < 0) {
        return std::nullopt;
    }
    return cursor;
}

// ---------------------------------------------------------------------------
// get_level_objects (LEVEL-DESIGN-SPEC.md 4.3.2)
// ---------------------------------------------------------------------------

/// One page of the level, as save strings.
///
/// One object per slice: `getSaveString` builds a string per call, and a 2000-object
/// page in a single frame would be the stall SPEC.md 6.1.4 exists to prevent. The
/// revision is checked on every slice, since the user can edit between frames.
class GetLevelObjectsJob final : public Job {
public:
    GetLevelObjectsJob(std::optional<Cursor> cursor, int limit)
        : m_cursor(cursor), m_limit(limit) {}

    bool step() override {
        auto* editor = LevelEditorLayer::get();
        if (editor == nullptr) {
            return fail("EDITOR_NOT_OPEN", "No editor layer is active.");
        }
        auto const revision = editor::LevelRevision::get().current();

        if (!m_started) {
            m_started = true;
            m_revision = revision;
            if (m_cursor.has_value()) {
                if (m_cursor->revision != revision) {
                    return fail(
                        "LEVEL_CHANGING",
                        "The level changed since the first page; start again without a cursor."
                    );
                }
                m_index = m_cursor->offset;
            }
            auto* objects = editor->getAllObjects();
            m_total = objects == nullptr ? 0 : static_cast<int>(objects->count());
            m_end = std::min(m_total, m_index + m_limit);
            m_records = matjson::Value::array();
        } else if (revision != m_revision) {
            return fail("LEVEL_CHANGING", "The level changed while it was being read.");
        }

        if (m_index >= m_end) {
            m_done = true;
            return true;
        }

        auto* objects = editor->getAllObjects();
        if (objects == nullptr || static_cast<int>(objects->count()) != m_total) {
            // The revision should have caught this first; checked anyway because an
            // out-of-range index here is a crash, not a wrong answer.
            return fail("LEVEL_CHANGING", "The level changed while it was being read.");
        }

        auto* object = static_cast<GameObject*>(objects->objectAtIndex(static_cast<unsigned int>(m_index)));
        ++m_index;
        if (object == nullptr) {
            return false;
        }

        // The named properties go through the same reader as `get_object_properties`,
        // so the two requests can never disagree about where an object is or how it
        // is turned -- whatever the save string's own keys turn out to mean.
        std::vector<std::pair<std::string, editor::PropertyValue>> values;
        for (auto const name : kRecordProperties) {
            if (auto value = editor::readProperty(object, name)) {
                values.emplace_back(std::string(name), *value);
            }
        }

        auto record = matjson::Value::object();
        record["object_id"] = object->m_uniqueID;
        record["id"] = object->m_objectID;
        record["properties"] = editor::propertiesToJson(values);
        record["save"] = std::string(object->getSaveString(editor));
        m_records.push(std::move(record));
        return false;
    }

    JobOutcome takeOutcome() override {
        if (!m_errorCode.empty()) {
            return JobOutcome::failure(m_errorCode, m_errorMessage);
        }
        auto result = matjson::Value::object();
        result["objects"] = std::move(m_records);
        result["total"] = m_total;
        result["revision"] = m_revision;
        result["next_cursor"] = m_end < m_total
            ? matjson::Value(encodeCursor(m_end, m_revision))
            : matjson::Value(nullptr);
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "get_level_objects";
    }

private:
    bool fail(std::string code, std::string message) {
        m_errorCode = std::move(code);
        m_errorMessage = std::move(message);
        return true;
    }

    std::optional<Cursor> m_cursor;
    int m_limit;
    bool m_started = false;
    bool m_done = false;
    std::int64_t m_revision = 0;
    int m_index = 0;
    int m_end = 0;
    int m_total = 0;
    matjson::Value m_records;
    std::string m_errorCode;
    std::string m_errorMessage;
};

// ---------------------------------------------------------------------------
// describe_object_types (LEVEL-DESIGN-SPEC.md 4.2)
// ---------------------------------------------------------------------------

/// Describes object types from instances that never enter the level.
///
/// `GameObject::createWithKey` builds a standalone, autoreleased object. It is not
/// added to the editor, so the level and the undo stack are untouched, and it is
/// released with the frame's autorelease pool. Whether that is safe for *every*
/// id is exactly what spike S-L2 establishes.
class DescribeObjectTypesJob final : public Job {
public:
    DescribeObjectTypesJob(std::optional<std::vector<int>> ids, int offset, int limit)
        : m_requested(std::move(ids)), m_offset(offset), m_limit(limit) {}

    bool step() override {
        if (!m_started) {
            m_started = true;
            if (m_requested.has_value()) {
                m_ids = std::move(*m_requested);
                m_total = static_cast<int>(m_ids.size());
                m_nextOffset = -1;
            } else {
                auto all = editor::catalogIds();
                m_total = static_cast<int>(all.size());
                auto const start = std::clamp(m_offset, 0, m_total);
                auto const end = std::min(start + m_limit, m_total);
                m_ids.assign(all.begin() + start, all.begin() + end);
                m_nextOffset = end < m_total ? end : -1;
            }
            m_entries = matjson::Value::array();
            m_failed = matjson::Value::array();
        }

        if (m_index >= m_ids.size()) {
            return true;
        }
        auto const id = m_ids[m_index++];

        if (isDenylisted(id)) {
            m_failed.push(id);
            return false;
        }

        // Logged before creation on purpose: if an id crashes the game, this is the
        // last line in the log, and it names the id for the denylist.
        log::info("describe_object_types: id {}", id);
        auto* object = GameObject::createWithKey(id);
        if (object == nullptr) {
            m_failed.push(id);
            return false;
        }

        // `createWithKey` only builds the sprite. The per-id setup -- the gameplay
        // type, the hitbox size, the radius -- happens in `customSetup`, which the
        // editor and the level loader call after creating an object. Without it a
        // spike reads as type 0 (solid) with a full 30x30 box, as the M0
        // foundations check showed.
        object->customSetup();
        // After setup, so the rect is recomputed from the real hitbox size.
        object->setPosition({ 0.f, 0.f });
        auto const position = object->getPosition();
        auto const& rect = object->getObjectRect();

        auto hitbox = matjson::Value::object();
        hitbox["x"] = static_cast<double>(rect.origin.x - position.x);
        hitbox["y"] = static_cast<double>(rect.origin.y - position.y);
        hitbox["w"] = static_cast<double>(std::max(0.f, rect.size.width));
        hitbox["h"] = static_cast<double>(std::max(0.f, rect.size.height));

        auto const size = object->getContentSize();
        auto visual = matjson::Value::object();
        visual["w"] = static_cast<double>(std::max(0.f, size.width));
        visual["h"] = static_cast<double>(std::max(0.f, size.height));

        auto entry = matjson::Value::object();
        entry["id"] = id;
        entry["object_type"] = static_cast<int>(object->m_objectType);
        entry["hitbox"] = std::move(hitbox);
        entry["radius"] = static_cast<double>(std::max(0.f, object->m_objectRadius));
        entry["visual"] = std::move(visual);
        entry["is_trigger"] = object->m_isTrigger;
        entry["is_decoration"] = object->m_isDecoration;
        entry["has_detail_color"] = object->m_detailColor != nullptr;
        entry["default_z_layer"] = static_cast<int>(object->m_defaultZLayer);
        m_entries.push(std::move(entry));
        return false;
    }

    JobOutcome takeOutcome() override {
        auto result = matjson::Value::object();
        result["entries"] = std::move(m_entries);
        result["total"] = m_total;
        result["next_offset"] = m_nextOffset >= 0 ? matjson::Value(m_nextOffset)
                                                  : matjson::Value(nullptr);
        result["failed"] = std::move(m_failed);
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "describe_object_types";
    }

private:
    std::optional<std::vector<int>> m_requested;
    int m_offset;
    int m_limit;
    bool m_started = false;
    std::vector<int> m_ids;
    std::size_t m_index = 0;
    int m_total = 0;
    int m_nextOffset = -1;
    matjson::Value m_entries;
    matjson::Value m_failed;
};

std::optional<int> optionalInt(matjson::Value const& payload, char const* key, int fallback) {
    if (!payload.contains(key) || payload[key].isNull()) {
        return fallback;
    }
    auto value = payload[key].asInt();
    if (!value) {
        return std::nullopt;
    }
    return static_cast<int>(value.unwrap());
}

} // namespace

bool isDesignRequest(std::string_view type) {
    return type == "get_level_objects" || type == "describe_object_types";
}

std::unique_ptr<Job> makeDesignJob(std::string_view type, matjson::Value const& payload) {
    if (type == "get_level_objects") {
        auto limit = optionalInt(payload, "limit", kDefaultLevelPage);
        if (!limit || *limit < 1 || *limit > kMaxLevelPage) {
            return invalid(fmt::format("'limit' must be an integer from 1 to {}", kMaxLevelPage));
        }
        std::optional<Cursor> cursor;
        if (payload.contains("cursor") && !payload["cursor"].isNull()) {
            auto text = payload["cursor"].asString();
            if (!text) {
                return invalid("'cursor' must be a string");
            }
            cursor = decodeCursor(text.unwrap());
            if (!cursor) {
                return invalid("'cursor' is not one this mod issued");
            }
        }
        return std::make_unique<GetLevelObjectsJob>(cursor, *limit);
    }

    if (type == "describe_object_types") {
        auto offset = optionalInt(payload, "offset", 0);
        auto limit = optionalInt(payload, "limit", kDefaultDescribePage);
        if (!offset || *offset < 0) {
            return invalid("'offset' must be a non-negative integer");
        }
        if (!limit || *limit < 1 || *limit > kMaxDescribePage) {
            return invalid(fmt::format("'limit' must be an integer from 1 to {}", kMaxDescribePage));
        }
        std::optional<std::vector<int>> ids;
        if (payload.contains("ids")) {
            auto const& array = payload["ids"];
            if (!array.isArray() || array.size() > static_cast<std::size_t>(kMaxDescribePage)) {
                return invalid(fmt::format("'ids' must be an array of at most {} integers", kMaxDescribePage));
            }
            std::vector<int> parsed;
            for (auto const& value : array) {
                auto id = value.asInt();
                if (!id) {
                    return invalid("'ids' must contain integers");
                }
                parsed.push_back(static_cast<int>(id.unwrap()));
            }
            ids = std::move(parsed);
        }
        return std::make_unique<DescribeObjectTypesJob>(std::move(ids), *offset, *limit);
    }

    return nullptr;
}

} // namespace gdai::exec
