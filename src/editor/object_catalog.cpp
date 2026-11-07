#include "object_catalog.hpp"

#include <Geode/binding/ObjectToolbox.hpp>

#include <algorithm>
#include <string>
#include <vector>

using namespace geode::prelude;

namespace gdai::editor {
namespace {

/// Guard on a single page.
///
/// A page is what the caller asked for, but the value comes off the wire, so it is
/// clamped here rather than trusted. Large enough that the sidecar fetches the whole
/// table in a handful of round trips, small enough that one frame stays far from the
/// protocol's 1 MB limit.
constexpr int kMaxPageSize = 1000;

/// The game's object table, or null before resources are loaded.
///
/// `sharedState()` builds the toolbox on first use, so calling it early is not a
/// crash -- it is simply an empty table, which is why `catalogAvailable` reports on
/// the table's contents rather than on `sharedState()` being non-null.
ObjectToolbox* toolbox() {
    return ObjectToolbox::sharedState();
}

/// Every id the game knows, ascending.
///
/// Sorted rather than taken in map order for a reason: paging hands the caller an
/// offset, and an unstable order would make a second page overlap the first or skip
/// entries depending on nothing the caller controls.
std::vector<int> allIds(ObjectToolbox* box) {
    std::vector<int> ids;
    ids.reserve(box->m_allKeys.size());
    for (auto const& entry : box->m_allKeys) {
        ids.push_back(entry.first);
    }
    std::sort(ids.begin(), ids.end());
    ids.erase(std::unique(ids.begin(), ids.end()), ids.end());
    return ids;
}

} // namespace

std::vector<int> catalogIds() {
    auto* box = toolbox();
    return box == nullptr ? std::vector<int>{} : allIds(box);
}

bool catalogAvailable() {
    auto* box = toolbox();
    return box != nullptr && !box->m_allKeys.empty();
}

CatalogPage takeCatalogPage(int offset, int limit) {
    CatalogPage page;
    auto* box = toolbox();
    if (box == nullptr) {
        log::warn("catalog: no ObjectToolbox yet");
        return page;
    }

    auto const ids = allIds(box);
    page.total = static_cast<int>(ids.size());
    if (page.total == 0) {
        // Logged once per call rather than silently: an empty table is the difference
        // between "the game has no objects" (impossible) and "this ran before the
        // toolbox was populated", and only the log tells them apart.
        log::warn("catalog: the object table is empty");
        return page;
    }

    auto const start = std::clamp(offset, 0, page.total);
    auto const count = std::clamp(limit > 0 ? limit : kMaxPageSize, 1, kMaxPageSize);
    auto const end = std::min(start + count, page.total);

    page.entries.reserve(static_cast<std::size_t>(end - start));
    for (int i = start; i < end; ++i) {
        auto const id = ids[static_cast<std::size_t>(i)];
        CatalogEntry entry;
        entry.id = id;

        // `intKeyToFrame` is the game's own lookup, so it is asked first. The map value
        // is used as a fallback because the two are populated from the same data and
        // one of them being empty for an id is not a reason to report nothing.
        if (auto frame = box->intKeyToFrame(id); frame != nullptr && frame[0] != '\0') {
            entry.frame = frame;
        } else if (auto const found = box->m_allKeys.find(id); found != box->m_allKeys.end()) {
            entry.frame = found->second;
        }
        entry.gridSize = box->gridNodeSizeForKey(id);
        page.entries.push_back(std::move(entry));
    }

    page.nextOffset = end < page.total ? end : -1;
    return page;
}

matjson::Value catalogPageToJson(CatalogPage const& page) {
    auto entries = matjson::Value::array();
    for (auto const& entry : page.entries) {
        auto json = matjson::Value::object();
        json["id"] = entry.id;
        if (!entry.frame.empty()) {
            json["frame"] = entry.frame;
        }
        if (entry.gridSize > 0.f) {
            json["grid_size"] = static_cast<double>(entry.gridSize);
        }
        entries.push(std::move(json));
    }

    auto result = matjson::Value::object();
    result["entries"] = std::move(entries);
    result["total"] = page.total;
    // Explicit null rather than an absent key: "there is no next page" is a fact the
    // caller must be able to tell apart from "the field was forgotten".
    result["next_offset"] = page.nextOffset >= 0 ? matjson::Value(page.nextOffset)
                                                 : matjson::Value(nullptr);
    return result;
}

} // namespace gdai::editor
