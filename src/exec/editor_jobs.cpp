#include "editor_jobs.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/EditorUI.hpp>
#include <Geode/binding/GameObject.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>

#include "../editor/properties.hpp"
#include "../editor/save_properties.hpp"
#include "../editor/transaction.hpp"

#include <algorithm>
#include <array>
#include <optional>
#include <utility>
#include <vector>

using namespace geode::prelude;

namespace gdai::exec {
namespace {

using editor::PropertyValue;

/// A request refused during validation, before any mutation.
///
/// `makeJob` returns one of these instead of failing outright, so the caller has
/// a single submit-and-await path whether or not the payload was acceptable.
class ErrorJob final : public Job {
public:
    ErrorJob(std::string code, std::string message)
        : m_code(std::move(code)), m_message(std::move(message)) {}

    bool step() override {
        return true;
    }

    JobOutcome takeOutcome() override {
        return JobOutcome::failure(m_code, m_message);
    }

    std::string_view type() const override {
        return "error";
    }

private:
    std::string m_code;
    std::string m_message;
};

std::optional<std::vector<std::int32_t>> parseIdList(matjson::Value const& array) {
    if (!array.isArray()) {
        return std::nullopt;
    }
    std::vector<std::int32_t> ids;
    for (auto const& entry : array) {
        auto value = entry.asInt();
        if (!value.ok()) {
            return std::nullopt;
        }
        ids.push_back(static_cast<std::int32_t>(value.unwrap()));
    }
    return ids;
}

/// Resolves a unique ID to a live object, or null.
///
/// Re-checks the ID rather than trusting the lookup, because a slot can be
/// recycled and hand back an object whose ID no longer matches
/// (SPEC.md 6.1.3 rule 5).
GameObject* resolve(LevelEditorLayer* editor, std::int32_t uniqueId) {
    auto* object = editor->findGameObject(uniqueId);
    if (object == nullptr || object->m_uniqueID != uniqueId) {
        return nullptr;
    }
    return object;
}

/// Base for jobs that touch the editor.
///
/// The editor pointer is resolved **inside `step()`**, never captured at
/// construction: jobs are built on the async runtime thread, where touching GD
/// state is forbidden (SPEC.md 6.1.3 rule 1/2), and the editor can also close
/// between slices, which would leave a cached pointer dangling (rule 5).
class EditorJob : public Job {
protected:
    LevelEditorLayer* editor() const {
        return LevelEditorLayer::get();
    }

    /// Returns true when the job must stop because there is no editor.
    bool checkEditor() {
        if (editor() != nullptr) {
            return false;
        }
        m_editorClosed = true;
        m_finished = true;
        return true;
    }

    JobOutcome editorClosedOutcome() const {
        return JobOutcome::failure("EDITOR_NOT_OPEN", "No editor layer is active.");
    }

    bool m_finished = false;
    bool m_editorClosed = false;
};

// ---------------------------------------------------------------------------
// create_objects
// ---------------------------------------------------------------------------

struct CreateRequest {
    std::int32_t type = 0;
    double x = 0.0;
    double levelY = 0.0;
    std::vector<std::pair<std::string, PropertyValue>> properties;
};

class CreateObjectsJob final : public EditorJob {
public:
    explicit CreateObjectsJob(std::vector<CreateRequest> requests)
        : m_requests(std::move(requests)) {}

    bool step() override {
        if (checkEditor()) {
            return true;
        }
        // One object per slice: this is what keeps a 1000-object request inside
        // the frame budget rather than stalling a frame (SPEC.md 6.1.4, A1).
        if (m_index >= m_requests.size()) {
            return true;
        }

        auto* ed = editor();
        auto const& request = m_requests[m_index];
        ++m_index;

        auto* object = ed->createObject(
            static_cast<int>(request.type),
            cocos2d::CCPoint {
                static_cast<float>(request.x),
                static_cast<float>(editor::levelYToCocos(request.levelY)),
            },
            // noUndo: the mod does not claim undo entries; revert is the
            // transaction's job (D2, SPEC.md 6.1.5).
            true
        );

        if (object == nullptr) {
            // Not creatable. Recorded and skipped rather than aborting the batch:
            // the rest is still useful, and the counts report what happened.
            ++m_failed;
            return false;
        }

        for (auto const& [name, value] : request.properties) {
            if (auto written = editor::writeProperty(object, name, value); !written) {
                log::warn("create_objects: property '{}' failed: {}", name, written.unwrapErr());
                ++m_failed;
            }
        }

        editor::TransactionManager::get().recordCreated(object->m_uniqueID);
        m_created.push_back(object->m_uniqueID);
        return false;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }
        auto result = matjson::Value::object();
        auto created = matjson::Value::array();
        for (auto const id : m_created) {
            created.push(id);
        }
        result["created"] = std::move(created);
        result["count"] = static_cast<int>(m_created.size());
        if (m_failed > 0) {
            result["failed"] = m_failed;
        }
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "create_objects";
    }

private:
    std::vector<CreateRequest> m_requests;
    std::vector<std::int32_t> m_created;
    std::size_t m_index = 0;
    int m_failed = 0;
};

// ---------------------------------------------------------------------------
// modify_objects
// ---------------------------------------------------------------------------

struct ModifyRequest {
    std::int32_t uniqueId = 0;
    std::vector<std::pair<std::string, PropertyValue>> set;
    std::vector<std::pair<std::string, double>> deltas;
};

class ModifyObjectsJob final : public EditorJob {
public:
    explicit ModifyObjectsJob(std::vector<ModifyRequest> requests)
        : m_requests(std::move(requests)) {}

    bool step() override {
        if (checkEditor()) {
            return true;
        }
        if (m_index >= m_requests.size()) {
            return true;
        }

        auto* ed = editor();
        auto const& request = m_requests[m_index];
        ++m_index;

        auto* object = resolve(ed, request.uniqueId);
        if (object == nullptr) {
            m_missing.push_back(request.uniqueId);
            return false;
        }

        // Prior values are captured once per object, before the first change, so
        // a turn that modifies the same object repeatedly still reverts to the
        // state it began with.
        auto& transactions = editor::TransactionManager::get();
        if (!transactions.hasModified(request.uniqueId)) {
            std::vector<std::pair<std::string, PropertyValue>> prior;
            for (auto const& [name, value] : request.set) {
                if (auto current = editor::readProperty(object, name)) {
                    prior.emplace_back(name, *current);
                }
            }
            for (auto const& [name, delta] : request.deltas) {
                if (auto current = editor::readProperty(object, name)) {
                    prior.emplace_back(name, *current);
                }
            }
            transactions.recordModified(request.uniqueId, std::move(prior));
        }

        auto changed = 0;
        for (auto const& [name, value] : request.set) {
            if (auto written = editor::writeProperty(object, name, value)) {
                ++changed;
            } else {
                log::warn("modify_objects: '{}' failed: {}", name, written.unwrapErr());
            }
        }
        for (auto const& [name, delta] : request.deltas) {
            if (auto applied = editor::applyDelta(object, name, delta)) {
                ++changed;
            } else {
                log::warn("modify_objects: delta '{}' failed: {}", name, applied.unwrapErr());
            }
        }

        if (changed > 0) {
            ++m_modified;
        }
        return false;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }
        auto result = matjson::Value::object();
        result["modified_count"] = m_modified;
        auto missing = matjson::Value::array();
        for (auto const id : m_missing) {
            missing.push(id);
        }
        result["missing"] = std::move(missing);

        // OBJECT_NOT_FOUND only when nothing at all matched; a partial match is a
        // normal outcome (SPEC.md 7.5).
        if (m_modified == 0 && !m_missing.empty()) {
            return JobOutcome::failure("OBJECT_NOT_FOUND", "No matching objects.");
        }
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "modify_objects";
    }

private:
    std::vector<ModifyRequest> m_requests;
    std::vector<std::int32_t> m_missing;
    std::size_t m_index = 0;
    int m_modified = 0;
};

// ---------------------------------------------------------------------------
// delete_objects
// ---------------------------------------------------------------------------

class DeleteObjectsJob final : public EditorJob {
public:
    explicit DeleteObjectsJob(std::vector<std::int32_t> targets)
        : m_targets(std::move(targets)) {}

    bool step() override {
        if (checkEditor()) {
            return true;
        }
        if (m_index >= m_targets.size()) {
            return true;
        }

        auto* ed = editor();
        auto const uniqueId = m_targets[m_index];
        ++m_index;

        auto* object = resolve(ed, uniqueId);
        if (object == nullptr) {
            m_missing.push_back(uniqueId);
            return false;
        }

        // Fine-grained diagnostics. A crash here killed the game with no crash
        // log, so each step is logged to localise it rather than guessing.
        log::info("delete: id {} resolved, reading save string", uniqueId);
        // The save string is captured BEFORE removal: it is the only way to
        // restore the object, and it is unavailable once the object is gone.
        auto const saveString = object->getSaveString(ed);
        log::info("delete: save string is {} chars", saveString.size());

        editor::TransactionManager::get().recordDeleted(saveString, uniqueId);

        // `noUndo = false` here, deliberately -- the one parameter held constant
        // across every previous crash was `true`.
        //
        // Every removal that corrupted the heap passed `noUndo = true`, which
        // asks the game to skip undo bookkeeping. If that fast path also skips
        // un-registration the object needs (section lists, group membership),
        // the omission would corrupt the heap in exactly this way. Letting the
        // game record the removal normally is the untested alternative.
        //
        // Cost if it works: the removal lands on the user's undo stack, which
        // D2 wanted to avoid. A groupable removal (`UndoCommand::DeleteMulti`
        // exists) could then be collapsed per transaction.
        log::info("delete: calling removeObject(noUndo=false)");
        ed->removeObject(object, false);
        log::info("delete: id {} removed", uniqueId);
        ++m_deleted;
        return false;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }
        auto result = matjson::Value::object();
        result["deleted_count"] = m_deleted;
        auto missing = matjson::Value::array();
        for (auto const id : m_missing) {
            missing.push(id);
        }
        result["missing"] = std::move(missing);

        if (m_deleted == 0 && !m_missing.empty()) {
            return JobOutcome::failure("OBJECT_NOT_FOUND", "No matching objects.");
        }
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "delete_objects";
    }

private:
    std::vector<std::int32_t> m_targets;
    std::vector<std::int32_t> m_missing;
    std::size_t m_index = 0;
    int m_deleted = 0;
};

// ---------------------------------------------------------------------------
// set_selection
// ---------------------------------------------------------------------------

class SetSelectionJob final : public EditorJob {
public:
    explicit SetSelectionJob(std::vector<std::int32_t> targets)
        : m_targets(std::move(targets)) {}

    bool step() override {
        if (checkEditor()) {
            return true;
        }
        if (m_finished) {
            return true;
        }
        m_finished = true;

        auto* ed = editor();
        // IDs are resolved first, so the selection can never contain a stale
        // object.
        auto* selection = cocos2d::CCArray::create();
        for (auto const uniqueId : m_targets) {
            if (auto* object = resolve(ed, uniqueId)) {
                selection->addObject(object);
            } else {
                m_missing.push_back(uniqueId);
            }
        }
        m_applied = selection;
        m_selectedCount = static_cast<int>(selection->count());
        return true;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }
        if (m_selectedCount == 0) {
            return JobOutcome::failure("OBJECT_NOT_FOUND", "No matching objects.");
        }

        if (auto* ed = editor(); ed != nullptr && ed->m_editorUI != nullptr) {
            ed->m_editorUI->selectObjects(m_applied, true);
        }

        auto result = matjson::Value::object();
        result["selected_count"] = m_selectedCount;
        auto missing = matjson::Value::array();
        for (auto const id : m_missing) {
            missing.push(id);
        }
        result["missing"] = std::move(missing);
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "set_selection";
    }

private:
    std::vector<std::int32_t> m_targets;
    std::vector<std::int32_t> m_missing;
    cocos2d::CCArray* m_applied = nullptr;
    int m_selectedCount = 0;
};

// ---------------------------------------------------------------------------
// get_object_properties
// ---------------------------------------------------------------------------

/// The properties the wire schema accepts, which is what this reports
/// (SPEC.md 7.5). `getSaveString` holds far more, including trigger-specific
/// keys, but those are not in the schema.
/// Properties reported for one object by `get_object_properties`.
///
/// A second copy of the property list, and it drifted: `color_channel_1` and
/// `color_channel_2` were added to the mod's validator and the tool schema but not
/// here, so the model was told a colourable object had **no colour channels at all**
/// and concluded it had no way to set colour. The values were readable the whole
/// time; this list was the only thing standing between them and the wire.
constexpr std::array<std::string_view, 13> kReportedProperties {{
    "x", "y", "rotation", "scale_x", "scale_y", "flip_x", "flip_y",
    "z_layer", "z_order", "editor_layer", "groups",
    "color_channel_1", "color_channel_2",
}};

class GetObjectPropertiesJob final : public EditorJob {
public:
    explicit GetObjectPropertiesJob(std::int32_t uniqueId) : m_uniqueId(uniqueId) {}

    bool step() override {
        if (checkEditor()) {
            return true;
        }
        if (m_finished) {
            return true;
        }
        m_finished = true;

        auto* object = resolve(editor(), m_uniqueId);
        if (object == nullptr) {
            return true;
        }

        std::vector<std::pair<std::string, PropertyValue>> values;
        for (auto const name : kReportedProperties) {
            if (auto value = editor::readProperty(object, name)) {
                values.emplace_back(std::string(name), *value);
            }
        }
        m_values = editor::propertiesToJson(values);
        m_found = true;
        return true;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }
        if (!m_found) {
            return JobOutcome::failure("OBJECT_NOT_FOUND", "No matching objects.");
        }
        auto result = matjson::Value::object();
        result["properties"] = std::move(m_values);
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "get_object_properties";
    }

private:
    std::int32_t m_uniqueId;
    matjson::Value m_values;
    bool m_found = false;
};

// ---------------------------------------------------------------------------
// query_objects and summarize_region
// ---------------------------------------------------------------------------

struct QueryFilters {
    std::optional<std::int32_t> typeId;
    std::optional<std::vector<std::int32_t>> uniqueIds;
    std::optional<std::int32_t> editorLayer;
    std::optional<std::int32_t> zLayer;
    std::optional<std::int32_t> group;
    std::optional<std::pair<double, double>> xRange;
    std::optional<std::pair<double, double>> yRange;
    bool selectedOnly = false;
};

/// Positional filters, shared by query and summarize.
///
/// Ranges compare against **wire (level) coordinates**, so a caller's `y_range`
/// means what the editor displays rather than the cocos position.
bool matchesPositional(QueryFilters const& filters, GameObject* object) {
    if (filters.editorLayer.has_value()
        && object->m_editorLayer != *filters.editorLayer) {
        return false;
    }
    if (filters.xRange.has_value()) {
        auto const& [min, max] = *filters.xRange;
        if (object->getPositionX() < min || object->getPositionX() > max) {
            return false;
        }
    }
    if (filters.yRange.has_value()) {
        auto const levelY = editor::cocosYToLevel(object->getPositionY());
        auto const& [min, max] = *filters.yRange;
        if (levelY < min || levelY > max) {
            return false;
        }
    }
    return true;
}

bool matchesAll(QueryFilters const& filters, GameObject* object) {
    if (filters.typeId.has_value() && object->m_objectID != *filters.typeId) {
        return false;
    }
    if (filters.zLayer.has_value()
        && static_cast<std::int32_t>(object->m_zLayer) != *filters.zLayer) {
        return false;
    }
    if (filters.uniqueIds.has_value()) {
        auto const& ids = *filters.uniqueIds;
        if (std::find(ids.begin(), ids.end(), object->m_uniqueID) == ids.end()) {
            return false;
        }
    }
    if (filters.group.has_value()) {
        auto const groups = editor::readProperty(object, "groups");
        if (!groups.has_value()) {
            return false;
        }
        auto const& list = groups->integers;
        if (std::find(list.begin(), list.end(), *filters.group) == list.end()) {
            return false;
        }
    }
    return matchesPositional(filters, object);
}

class QueryObjectsJob final : public EditorJob {
public:
    QueryObjectsJob(QueryFilters filters, std::size_t offset, std::size_t limit)
        : m_filters(std::move(filters)), m_offset(offset), m_limit(limit) {}

    bool step() override {
        if (checkEditor()) {
            return true;
        }
        if (m_finished) {
            return true;
        }
        m_finished = true;

        auto* ed = editor();
        // `selected_only` reads the selection instead of the whole object list.
        std::vector<GameObject*> selected;
        if (m_filters.selectedOnly && ed->m_editorUI != nullptr) {
            if (auto* current = ed->m_editorUI->getSelectedObjects()) {
                for (unsigned int i = 0; i < current->count(); ++i) {
                    selected.push_back(static_cast<GameObject*>(current->objectAtIndex(i)));
                }
            }
        }

        auto* objects = ed->getAllObjects();
        auto const examine = [this](GameObject* object) {
            if (object == nullptr || !matchesAll(m_filters, object)) {
                return;
            }
            ++m_totalMatches;
            if (m_totalMatches > m_offset && m_results.size() < m_limit) {
                m_results.push_back(toRecord(object));
            }
        };

        if (m_filters.selectedOnly) {
            for (auto* object : selected) {
                examine(object);
            }
        } else if (objects != nullptr) {
            for (unsigned int i = 0; i < objects->count(); ++i) {
                examine(static_cast<GameObject*>(objects->objectAtIndex(i)));
            }
        }

        return true;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }
        auto result = matjson::Value::object();
        auto objects = matjson::Value::array();
        for (auto& record : m_results) {
            objects.push(std::move(record));
        }
        result["objects"] = std::move(objects);
        result["total_matches"] = static_cast<int>(m_totalMatches);

        auto const returned = m_offset + m_results.size();
        auto const truncated = returned < m_totalMatches;
        result["truncated"] = truncated;
        if (truncated) {
            // An opaque offset cursor; the caller passes it back to continue.
            result["next_cursor"] = std::to_string(returned);
        } else {
            result["next_cursor"] = matjson::Value(nullptr);
        }
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "query_objects";
    }

private:
    static matjson::Value toRecord(GameObject* object) {
        auto record = matjson::Value::object();
        record["object_id"] = object->m_uniqueID;
        record["id"] = object->m_objectID;
        record["x"] = object->getPositionX();
        record["y"] = editor::cocosYToLevel(object->getPositionY());
        record["rotation"] = object->getRotation();
        return record;
    }

    QueryFilters m_filters;
    std::size_t m_offset;
    std::size_t m_limit;
    std::vector<matjson::Value> m_results;
    std::size_t m_totalMatches = 0;
};

class SummarizeRegionJob final : public EditorJob {
public:
    explicit SummarizeRegionJob(QueryFilters filters) : m_filters(std::move(filters)) {}
    bool step() override {
        if (checkEditor()) {
            return true;
        }
        if (m_finished) {
            return true;
        }
        m_finished = true;

        auto* objects = editor()->getAllObjects();
        if (objects == nullptr) {
            return true;
        }

        for (unsigned int i = 0; i < objects->count(); ++i) {
            auto* object = static_cast<GameObject*>(objects->objectAtIndex(i));
            if (object == nullptr || !matchesPositional(m_filters, object)) {
                continue;
            }

            auto const levelY = editor::cocosYToLevel(object->getPositionY());
            if (m_total == 0) {
                m_minX = m_maxX = object->getPositionX();
                m_minY = m_maxY = levelY;
            } else {
                m_minX = std::min(m_minX, static_cast<double>(object->getPositionX()));
                m_maxX = std::max(m_maxX, static_cast<double>(object->getPositionX()));
                m_minY = std::min(m_minY, levelY);
                m_maxY = std::max(m_maxY, levelY);
            }
            ++m_total;

            auto const typeId = object->m_objectID;
            auto const found = std::find_if(m_byType.begin(), m_byType.end(),
                [typeId](auto const& entry) { return entry.first == typeId; });
            if (found != m_byType.end()) {
                ++found->second;
            } else {
                m_byType.emplace_back(typeId, 1);
            }
        }

        // Most common first, so the 20-entry cap keeps the informative types.
        std::sort(m_byType.begin(), m_byType.end(),
            [](auto const& lhs, auto const& rhs) { return lhs.second > rhs.second; });
        if (m_byType.size() > kMaxTypeEntries) {
            m_byType.resize(kMaxTypeEntries);
        }
        return true;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }
        auto result = matjson::Value::object();
        result["total"] = static_cast<int>(m_total);

        if (m_total > 0) {
            auto bounds = matjson::Value::object();
            bounds["x1"] = m_minX;
            bounds["y1"] = m_minY;
            bounds["x2"] = m_maxX;
            bounds["y2"] = m_maxY;
            result["bounds"] = std::move(bounds);
        }

        auto byType = matjson::Value::array();
        for (auto const& [typeId, count] : m_byType) {
            auto entry = matjson::Value::object();
            entry["id"] = typeId;
            entry["count"] = count;
            byType.push(std::move(entry));
        }
        result["by_type"] = std::move(byType);
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "summarize_region";
    }

private:
    /// `by_type` is capped at 20 entries (SPEC.md 7.5).
    static constexpr std::size_t kMaxTypeEntries = 20;

    QueryFilters m_filters;
    std::vector<std::pair<std::int32_t, int>> m_byType;
    std::size_t m_total = 0;
    double m_minX = 0.0;
    double m_minY = 0.0;
    double m_maxX = 0.0;
    double m_maxY = 0.0;
};

/// Reports the distinct object **types** present in the level.
///
/// This exists because there is no authoritative source of object names. The game
/// exposes `m_objectID` and nothing else -- no names, no categories -- so any
/// "ID 8 is a spike" table in the sidecar would be somebody's memory, and a wrong
/// guess silently places the wrong object.
///
/// What is available is far better: the types the user has actually built with.
/// Those ids are real, they exist in this level, and they are guaranteed creatable.
/// So the assistant can be shown "type 37 appears 12 times, roughly 60x30 units,
/// first at (300, 105)" and reuse it, instead of inventing an id.
///
/// The size is included on purpose: with a count, a bounding size and a position,
/// a model can tell a small repeated hazard from a wide platform without either of
/// them being named.
class ListObjectTypesJob final : public EditorJob {
public:
    bool step() override {
        if (checkEditor()) {
            return true;
        }
        if (m_finished) {
            return true;
        }
        m_finished = true;

        auto* objects = editor()->getAllObjects();
        if (objects == nullptr) {
            m_done = true;
            return true;
        }

        struct Accum {
            int count = 0;
            double width = 0.0;
            double height = 0.0;
            double sampleX = 0.0;
            double sampleY = 0.0;
        };
        std::vector<std::pair<std::int32_t, Accum>> types;

        auto const total = objects->count();
        for (unsigned int i = 0; i < total; ++i) {
            auto* object = static_cast<GameObject*>(objects->objectAtIndex(i));
            if (object == nullptr) {
                continue;
            }
            auto const typeId = object->m_objectID;

            auto const found = std::find_if(
                types.begin(), types.end(),
                [typeId](auto const& entry) { return entry.first == typeId; }
            );
            auto& slot = found != types.end() ? found->second : [&]() -> Accum& {
                types.emplace_back(typeId, Accum {});
                return types.back().second;
            }();

            // Record the first sighting's size and position as representative. A
            // later one may be scaled differently; averaging would describe no
            // actual object.
            // Each object's own level-space y, tracked separately from the per-type
            // sample. Using the type's first sighting here would report the lowest
            // *type* rather than the lowest object, which is a different number the
            // moment a type appears more than once.
            auto const objectY = editor::cocosYToLevel(object->getPositionY());
            if (!m_lowestYKnown || objectY < m_lowestY) {
                m_lowestY = objectY;
                m_lowestYKnown = true;
            }

            if (slot.count == 0) {
                slot.sampleX = object->getPositionX();
                slot.sampleY = objectY;
                if (auto const& rect = object->getObjectRect(); rect.size.width > 0.f) {
                    slot.width = rect.size.width;
                    slot.height = rect.size.height;
                }
            }
            ++slot.count;
        }

        // Most-used first: the level's vocabulary, in the order that is useful.
        std::sort(types.begin(), types.end(), [](auto const& lhs, auto const& rhs) {
            return lhs.second.count > rhs.second.count;
        });
        if (types.size() > kMaxTypes) {
            types.resize(kMaxTypes);
        }

        auto result = matjson::Value::object();
        result["total_objects"] = static_cast<int>(total);

        // The lowest object in the level, in level coordinates.
        //
        // This is the practical answer to "where is the ground": the ground line
        // cannot be read from `GJGroundLayer` (its `getGroundY()` returns 0 and its
        // node position tracks the camera, so it moves as you scroll -- measured
        // 102, then 153, on the same level). But the lowest thing the user has built
        // is stable, observable, and exactly what "on the ground" means in practice.
        //
        // Reported as a hint rather than as `ground_y`, because a level with nothing
        // near the ground has no meaningful lowest object.
        if (m_lowestYKnown) {
            result["lowest_y"] = m_lowestY;
        }

        auto listed = matjson::Value::array();
        for (auto const& [typeId, acc] : types) {
            auto entry = matjson::Value::object();
            entry["id"] = typeId;
            entry["count"] = acc.count;
            entry["width"] = acc.width;
            entry["height"] = acc.height;
            entry["sample_x"] = acc.sampleX;
            entry["sample_y"] = acc.sampleY;
            listed.push(std::move(entry));
        }
        result["types"] = std::move(listed);
        m_result = std::move(result);
        m_done = true;
        return true;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }
        return JobOutcome::success(matjson::Value(m_result));
    }

    std::string_view type() const override {
        return "list_object_types";
    }

private:
    static constexpr std::size_t kMaxTypes = 40;

    bool m_finished = false;
    bool m_done = false;
    bool m_lowestYKnown = false;
    double m_lowestY = 0.0;
    matjson::Value m_result = matjson::Value::object();
};

// ---------------------------------------------------------------------------
// payload parsing
// ---------------------------------------------------------------------------

/// Reads an optional `[min, max]`.
///
/// nullopt means malformed; an empty inner optional means absent.
std::optional<std::optional<std::pair<double, double>>> parseRange(
    matjson::Value const& json, char const* key
) {
    if (!json.contains(key)) {
        return std::optional<std::pair<double, double>> { std::nullopt };
    }
    auto const range = json[key];
    if (!range.isArray() || range.size() != 2) {
        return std::nullopt;
    }
    auto low = range[0].asDouble();
    auto high = range[1].asDouble();
    if (!low.ok() || !high.ok()) {
        return std::nullopt;
    }
    return std::optional<std::pair<double, double>> { { low.unwrap(), high.unwrap() } };
}

std::optional<QueryFilters> parseFilters(matjson::Value const& filters) {
    QueryFilters parsed;
    if (!filters.isObject()) {
        return std::nullopt;
    }

    auto const readInt = [&filters](char const* key) -> std::optional<std::int32_t> {
        auto value = filters[key].asInt();
        if (!value.ok()) {
            return std::nullopt;
        }
        return static_cast<std::int32_t>(value.unwrap());
    };

    for (auto const& entry : std::array<std::pair<char const*, std::optional<std::int32_t>*>, 4> {{
        { "id", &parsed.typeId },
        { "editor_layer", &parsed.editorLayer },
        { "z_layer", &parsed.zLayer },
        { "group", &parsed.group },
    }}) {
        if (!filters.contains(entry.first)) {
            continue;
        }
        auto value = readInt(entry.first);
        if (!value.has_value()) {
            return std::nullopt;
        }
        *entry.second = value;
    }

    if (filters.contains("ids")) {
        auto ids = parseIdList(filters["ids"]);
        if (!ids.has_value()) {
            return std::nullopt;
        }
        parsed.uniqueIds = std::move(*ids);
    }
    if (auto x = parseRange(filters, "x_range"); x.has_value()) {
        parsed.xRange = *x;
    } else {
        return std::nullopt;
    }
    if (auto y = parseRange(filters, "y_range"); y.has_value()) {
        parsed.yRange = *y;
    } else {
        return std::nullopt;
    }
    if (filters.contains("selected_only")) {
        auto value = filters["selected_only"].asBool();
        if (!value.ok()) {
            return std::nullopt;
        }
        parsed.selectedOnly = value.unwrap();
    }

    return parsed;
}

/// Hard maxima from SPEC.md 6.1.4, enforced here regardless of what the sidecar
/// sends (D10: the mod is the trust boundary).
constexpr std::size_t kMaxObjectsPerRequest = 5000;
constexpr std::size_t kMaxQueryPage = 500;

std::unique_ptr<Job> invalid(std::string message) {
    return std::make_unique<ErrorJob>("INVALID_REQUEST", std::move(message));
}

std::unique_ptr<Job> limitExceeded(std::string message) {
    return std::make_unique<ErrorJob>("LIMIT_EXCEEDED", std::move(message));
}

/// Splits a `CODE: message` string from the property layer into an error job, so
/// that `UNSUPPORTED_PROPERTY` stays distinguishable from `INVALID_REQUEST`.
std::unique_ptr<Job> propertyError(std::string const& error) {
    auto const colon = error.find(':');
    if (colon == std::string::npos) {
        return std::make_unique<ErrorJob>("INVALID_REQUEST", error);
    }
    auto message = error.substr(colon + 1);
    if (!message.empty() && message.front() == ' ') {
        message.erase(0, 1);
    }
    return std::make_unique<ErrorJob>(error.substr(0, colon), std::move(message));
}

} // namespace

std::unique_ptr<Job> makeErrorJob(std::string errorObject) {
    return std::make_unique<ErrorJob>("INVALID_REQUEST", std::move(errorObject));
}

// ---------------------------------------------------------------------------
// get_save_properties / set_save_property (SPEC.md 7.5.4)
// ---------------------------------------------------------------------------

/// Reports one object's raw save-string properties.
///
/// The values are deliberately **not** interpreted here. The sidecar has a generated
/// key-to-name table (SPEC.md 7.5.3) covering 511 keys across 48 classes, and it is the
/// only side that knows what key 51 means. The mod's job is to hand over the pairs.
class GetSavePropertiesJob final : public EditorJob {
public:
    explicit GetSavePropertiesJob(std::int32_t uniqueId) : m_uniqueId(uniqueId) {}

    bool step() override {
        if (checkEditor()) {
            return true;
        }
        if (m_finished) {
            return true;
        }
        m_finished = true;

        auto entries = editor::readSaveEntries(m_uniqueId);
        if (!entries) {
            m_error = entries.unwrapErr();
            return true;
        }
        m_entries = std::move(entries.unwrap());
        m_found = true;
        return true;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }
        if (!m_found) {
            // The read path already produces a coded message ("CODE: text"), and
            // `splitCode` in the bridge turns it back into code plus text -- the same
            // route the transaction requests use.
            return JobOutcome::failure(
                "OBJECT_NOT_FOUND",
                m_error.empty() ? "No matching objects." : m_error
            );
        }

        auto properties = matjson::Value::array();
        for (auto const& entry : m_entries) {
            auto item = matjson::Value::object();
            item["key"] = entry.key;
            item["value"] = entry.value;
            properties.push(std::move(item));
        }

        auto result = matjson::Value::object();
        result["object_id"] = m_uniqueId;
        result["properties"] = std::move(properties);
        result["count"] = static_cast<int>(m_entries.size());
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "get_save_properties";
    }

private:
    std::int32_t m_uniqueId;
    std::vector<editor::SaveEntry> m_entries;
    std::string m_error;
    bool m_found = false;
};

/// Sets one save-string key on every target, rebuilding each object.
///
/// One object per slice, like `create_objects`: rebuilding is remove-plus-create, which
/// is more expensive than a field write, so a batch of them must not land in one frame.
///
/// **The IDs change.** Each replacement is a new object with a new unique id, reported
/// back so the caller can carry on working with it.
class SetSavePropertyJob final : public EditorJob {
public:
    SetSavePropertyJob(std::vector<std::int32_t> targets, int key, std::string value)
        : m_targets(std::move(targets)), m_key(key), m_value(std::move(value)) {}

    bool step() override {
        if (checkEditor()) {
            return true;
        }
        if (m_index >= m_targets.size()) {
            return true;
        }

        auto const uniqueId = m_targets[m_index];
        ++m_index;

        auto rewritten = editor::rewriteSaveProperty(uniqueId, m_key, m_value);
        if (!rewritten) {
            log::warn(
                "set_save_property: object {} key {} failed: {}",
                uniqueId, m_key, rewritten.unwrapErr()
            );
            m_failures.push_back(uniqueId);
            return false;
        }

        auto const newId = rewritten.unwrap();
        if (newId == uniqueId) {
            // Nothing changed, which means the value was already what was asked for.
            ++m_unchanged;
            return false;
        }
        ++m_modified;
        m_replacements.emplace_back(uniqueId, newId);
        return false;
    }

    JobOutcome takeOutcome() override {
        if (m_editorClosed) {
            return editorClosedOutcome();
        }

        auto replacements = matjson::Value::array();
        for (auto const& [oldId, newId] : m_replacements) {
            auto item = matjson::Value::object();
            item["old_id"] = oldId;
            item["new_id"] = newId;
            replacements.push(std::move(item));
        }
        auto failed = matjson::Value::array();
        for (auto const id : m_failures) {
            failed.push(id);
        }

        auto result = matjson::Value::object();
        result["modified_count"] = m_modified;
        result["unchanged_count"] = m_unchanged;
        result["replacements"] = std::move(replacements);
        result["failed"] = std::move(failed);
        // Said plainly, because it is surprising and it matters: any id the caller was
        // holding for these objects is now stale, including ones it created this turn.
        result["ids_changed"] = true;

        if (m_modified == 0 && m_unchanged == 0 && !m_failures.empty()) {
            return JobOutcome::failure("OBJECT_NOT_FOUND", "No matching objects.");
        }
        return JobOutcome::success(std::move(result));
    }

    std::string_view type() const override {
        return "set_save_property";
    }

private:
    std::vector<std::int32_t> m_targets;
    int m_key;
    std::string m_value;
    std::vector<std::pair<std::int32_t, std::int32_t>> m_replacements;
    std::vector<std::int32_t> m_failures;
    std::size_t m_index = 0;
    int m_modified = 0;
    int m_unchanged = 0;
};

bool isImplementedRequest(std::string_view type) {
    return type == "create_objects" || type == "modify_objects" || type == "delete_objects"
        || type == "set_selection" || type == "query_objects" || type == "summarize_region"
        || type == "get_object_properties" || type == "list_object_types"
        || type == "get_save_properties" || type == "set_save_property"
        // The transaction requests are not queue jobs -- the bridge handles them with
        // a main-thread hop -- but they must be listed here, because this predicate is
        // the gate the bridge uses to reach *any* implemented request.
        //
        // They were missing for a long time, and the effect was total rather than
        // partial: `begin_transaction` fell through to "unhandled request type", the
        // tool loop treats a failed begin as fatal for that call, and so **no mutating
        // tool ever executed**. The assistant reported creating objects and created
        // nothing. It went unnoticed because the earlier end-to-end test ran with the
        // developer probes enabled, and the objects that appeared in the level were the
        // probe's, not the assistant's.
        || type == "begin_transaction" || type == "end_transaction"
        || type == "revert_transaction";
}

std::unique_ptr<Job> makeJob(std::string_view type, matjson::Value const& payload) {
    // NOTE: no editor access here. `makeJob` runs on the async runtime thread,
    // where touching GD state is forbidden (SPEC.md 6.1.3). Whether an editor is
    // open is checked inside each job's `step()`, on the main thread.

    if (type == "create_objects") {
        if (!payload.contains("objects") || !payload["objects"].isArray()) {
            return invalid("'objects' must be an array");
        }
        auto const objects = payload["objects"];
        if (objects.size() == 0) {
            return invalid("'objects' must not be empty");
        }
        if (objects.size() > kMaxObjectsPerRequest) {
            return limitExceeded(fmt::format(
                "between 1 and {} objects per request", kMaxObjectsPerRequest
            ));
        }

        // Validated whole before any mutation, so a bad entry cannot leave the
        // level half-changed (SPEC.md 6.1.4).
        std::vector<CreateRequest> requests;
        requests.reserve(objects.size());
        for (auto const& entry : objects) {
            if (!entry.isObject() || !entry.contains("id") || !entry.contains("x")
                || !entry.contains("y")) {
                return invalid("each object needs id, x and y");
            }
            auto id = entry["id"].asInt();
            auto x = entry["x"].asDouble();
            auto y = entry["y"].asDouble();
            if (!id.ok() || !x.ok() || !y.ok()) {
                return invalid("id, x and y must be numbers");
            }
            if (id.unwrap() <= 0 || id.unwrap() > 10000) {
                return std::make_unique<ErrorJob>(
                    "INVALID_OBJECT_ID",
                    fmt::format("object id {} is out of range", id.unwrap())
                );
            }

            CreateRequest request;
            request.type = static_cast<std::int32_t>(id.unwrap());
            request.x = x.unwrap();
            request.levelY = y.unwrap();

            if (entry.contains("properties")) {
                auto parsed = editor::parseProperties(entry["properties"]);
                if (!parsed) {
                    return propertyError(parsed.unwrapErr());
                }
                request.properties = std::move(parsed.unwrap());
            }
            requests.push_back(std::move(request));
        }

        return std::make_unique<CreateObjectsJob>(std::move(requests));
    }

    if (type == "list_object_types") {
        // No payload: it always describes the whole level.
        return std::make_unique<ListObjectTypesJob>();
    }

    if (type == "modify_objects") {
        if (!payload.contains("targets")) {
            return invalid("'targets' is required");
        }
        auto targets = parseIdList(payload["targets"]);
        if (!targets.has_value() || targets->empty()) {
            return invalid("'targets' must be a non-empty list of ids");
        }
        if (targets->size() > kMaxObjectsPerRequest) {
            return limitExceeded(fmt::format("at most {} targets", kMaxObjectsPerRequest));
        }
        if (!payload.contains("set") && !payload.contains("delta")) {
            return invalid("'set' or 'delta' is required");
        }

        std::vector<std::pair<std::string, PropertyValue>> setValues;
        if (payload.contains("set")) {
            auto parsed = editor::parseProperties(payload["set"]);
            if (!parsed) {
                return propertyError(parsed.unwrapErr());
            }
            setValues = std::move(parsed.unwrap());
        }

        std::vector<std::pair<std::string, double>> deltas;
        if (payload.contains("delta")) {
            auto const delta = payload["delta"];
            if (!delta.isObject()) {
                return invalid("'delta' must be an object");
            }
            for (auto const name : { "x", "y", "rotation" }) {
                if (!delta.contains(name)) {
                    continue;
                }
                auto value = delta[name].asDouble();
                if (!value.ok()) {
                    return invalid("delta values must be numbers");
                }
                deltas.emplace_back(name, value.unwrap());
            }
            if (delta.size() != deltas.size()) {
                // A delta on an unsupported property is refused rather than
                // ignored, matching SPEC.md 7.5's x/y/rotation restriction.
                return std::make_unique<ErrorJob>(
                    "UNSUPPORTED_PROPERTY", "only x, y and rotation support deltas"
                );
            }
        }

        std::vector<ModifyRequest> requests;
        requests.reserve(targets->size());
        for (auto const uniqueId : *targets) {
            requests.push_back(ModifyRequest { uniqueId, setValues, deltas });
        }
        return std::make_unique<ModifyObjectsJob>(std::move(requests));
    }

    if (type == "delete_objects") {
        if (!payload.contains("targets")) {
            return invalid("'targets' is required");
        }
        auto targets = parseIdList(payload["targets"]);
        if (!targets.has_value() || targets->empty()) {
            return invalid("'targets' must be a non-empty list of ids");
        }
        if (targets->size() > kMaxObjectsPerRequest) {
            return limitExceeded(fmt::format("at most {} targets", kMaxObjectsPerRequest));
        }
        return std::make_unique<DeleteObjectsJob>(std::move(*targets));
    }

    if (type == "set_selection") {
        if (!payload.contains("targets")) {
            return invalid("'targets' is required");
        }
        auto targets = parseIdList(payload["targets"]);
        if (!targets.has_value()) {
            return invalid("'targets' must be a list of ids");
        }
        return std::make_unique<SetSelectionJob>(std::move(*targets));
    }

    if (type == "get_object_properties") {
        if (!payload.contains("object_id")) {
            return invalid("'object_id' is required");
        }
        auto value = payload["object_id"].asInt();
        if (!value.ok()) {
            return invalid("'object_id' must be an integer");
        }
        return std::make_unique<GetObjectPropertiesJob>(static_cast<std::int32_t>(value.unwrap()));
    }

    if (type == "get_save_properties") {
        if (!payload.contains("object_id")) {
            return invalid("'object_id' is required");
        }
        auto value = payload["object_id"].asInt();
        if (!value.ok()) {
            return invalid("'object_id' must be an integer");
        }
        return std::make_unique<GetSavePropertiesJob>(static_cast<std::int32_t>(value.unwrap()));
    }

    if (type == "set_save_property") {
        if (!payload.contains("targets")) {
            return invalid("'targets' is required");
        }
        auto targets = parseIdList(payload["targets"]);
        if (!targets.has_value() || targets->empty()) {
            return invalid("'targets' must be a non-empty list of ids");
        }
        if (targets->size() > kMaxObjectsPerRequest) {
            return limitExceeded(fmt::format("at most {} targets", kMaxObjectsPerRequest));
        }
        if (!payload.contains("key")) {
            return invalid("'key' is required");
        }
        auto key = payload["key"].asInt();
        if (!key.ok() || key.unwrap() <= 0) {
            return invalid("'key' must be a positive integer save-string key");
        }
        // The value goes through as text. Coercing it here would mean re-deriving the
        // game's own encodings (dot-separated group lists, `a`-separated HSV, base64
        // text); the sidecar knows the type from the property table and sends the exact
        // text the save string should contain.
        if (!payload.contains("value")) {
            return invalid("'value' is required");
        }
        auto value = payload["value"];
        std::string text;
        if (auto asText = value.asString(); asText.ok()) {
            text = asText.unwrap();
        } else if (auto asInt = value.asInt(); asInt.ok()) {
            text = fmt::format("{}", asInt.unwrap());
        } else if (auto asDouble = value.asDouble(); asDouble.ok()) {
            text = fmt::format("{}", asDouble.unwrap());
        } else if (auto asBool = value.asBool(); asBool.ok()) {
            // The game writes booleans as 0/1, and `true` in a save string would be read
            // back as zero -- a wrong value rather than an error.
            text = asBool.unwrap() ? "1" : "0";
        } else {
            return invalid("'value' must be a string, number or boolean");
        }

        return std::make_unique<SetSavePropertyJob>(
            std::move(*targets), static_cast<int>(key.unwrap()), std::move(text)
        );
    }

    if (type == "query_objects") {
        auto const filtersJson = payload.contains("filters")
            ? payload["filters"]
            : matjson::Value::object();
        auto filters = parseFilters(filtersJson);
        if (!filters.has_value()) {
            return invalid("malformed filters");
        }

        std::size_t limit = 100;
        if (payload.contains("limit")) {
            auto value = payload["limit"].asInt();
            if (!value.ok() || value.unwrap() < 1) {
                return invalid("'limit' must be positive");
            }
            limit = std::min(static_cast<std::size_t>(value.unwrap()), kMaxQueryPage);
        }

        std::size_t offset = 0;
        if (payload.contains("cursor") && !payload["cursor"].isNull()) {
            auto text = payload["cursor"].asString();
            if (!text.ok()) {
                return invalid("malformed cursor");
            }
            try {
                offset = static_cast<std::size_t>(std::stoull(text.unwrap()));
            } catch (std::exception const&) {
                return invalid("malformed cursor");
            }
        }

        return std::make_unique<QueryObjectsJob>(std::move(*filters), offset, limit);
    }

    if (type == "summarize_region") {
        auto filters = parseFilters(payload);
        if (!filters.has_value()) {
            return invalid("malformed region");
        }
        return std::make_unique<SummarizeRegionJob>(std::move(*filters));
    }

    return nullptr;
}

} // namespace gdai::exec
