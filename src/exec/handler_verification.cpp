#include "handler_verification.hpp"

#include <Geode/Geode.hpp>
#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/utils/async.hpp>

#include "../editor/transaction.hpp"
#include "editor_jobs.hpp"
#include "job_queue.hpp"

#include <string>
#include <utility>

using namespace geode::prelude;

namespace gdai::exec {
namespace {

/// Set false to skip the standalone `delete_objects` step.
///
/// `removeObject` on a programmatically created object has crashed the game with
/// **STATUS_HEAP_CORRUPTION** (0xc0000374) on every attempt, and several
/// explanations have been tested and falsified: the coordinates, which removal
/// API is used, whether selection was involved, and the calling context
/// (scheduled selector vs. Geode's main-thread queue).
///
/// The one thing never tested is whether removal across frames works *at all*.
/// The transaction probe only ever removed objects created within the same
/// main-thread callback, so its success does not cover the cross-frame case.
///
/// Skipping the delete step lets the run reach the final revert, which removes
/// the created objects via the same `removeObject` but after frames have passed.
/// One run then separates two very different situations:
///
///   - revert succeeds  -> removal across frames is fine, and `delete_objects`
///     specifically is broken; the difference is then in how it is invoked.
///   - revert crashes   -> removal across frames is itself unsafe, which makes
///     this a constraint on the whole design rather than one handler.
constexpr bool kTestDeleteObjects = false;

/// Collects results so the run ends with a single summary rather than a wall of
/// interleaved lines.
struct Verifier {
    int passed = 0;
    int failed = 0;

    void check(bool condition, std::string_view label, std::string detail = {}) {
        if (condition) {
            ++passed;
            log::info("HANDLER-VERIFY PASS | {}", label);
            return;
        }
        ++failed;
        log::error(
            "HANDLER-VERIFY FAIL | {}{}{}",
            label, detail.empty() ? "" : " | ", detail
        );
    }

    void checkEqual(
        long long actual, long long expected, std::string_view label
    ) {
        check(
            actual == expected, label,
            fmt::format("expected {}, got {}", expected, actual)
        );
    }
};

/// Runs a function on the main thread and returns its result.
template <typename Fn>
auto onMain(Fn&& fn) {
    return async::waitForMainThread<std::invoke_result_t<Fn>>(std::forward<Fn>(fn));
}

arc::Future<int> objectCount() {
    auto count = co_await onMain([] {
        auto* editor = LevelEditorLayer::get();
        if (editor == nullptr) {
            return -1;
        }
        auto* objects = editor->getAllObjects();
        return objects == nullptr ? 0 : static_cast<int>(objects->count());
    });
    co_return count.value_or(-1);
}

/// Builds and submits a job, exactly as the bridge does for an inbound request.
arc::Future<JobOutcome> submit(std::string_view type, matjson::Value payload) {
    auto job = makeJob(type, payload);
    if (job == nullptr) {
        co_return JobOutcome::failure("INTERNAL_ERROR", "makeJob returned null");
    }
    co_return co_await JobQueue::get().submit(std::move(job));
}

/// Reads one property back through the real handler, so the read path is
/// exercised rather than the fields directly.
arc::Future<std::optional<double>> readNumber(std::int32_t uniqueId, std::string_view property) {
    auto payload = matjson::Value::object();
    payload["object_id"] = uniqueId;
    auto outcome = co_await submit("get_object_properties", std::move(payload));
    if (!outcome.ok || !outcome.payload.contains("properties")) {
        co_return std::nullopt;
    }
    auto const value = outcome.payload["properties"][std::string(property)].asDouble();
    if (!value.ok()) {
        co_return std::nullopt;
    }
    co_return value.unwrap();
}

matjson::Value objectEntry(int id, double x, double levelY) {
    auto entry = matjson::Value::object();
    entry["id"] = id;
    entry["x"] = x;
    entry["y"] = levelY;
    return entry;
}

/// Where the verification places its objects.
///
/// **Ordinary level coordinates, deliberately.** Placing objects at extreme
/// coordinates crashes `removeObject`: verified at (100, 8000) and again at
/// (200000, 750), while the transaction probe removes the same way without
/// trouble at x ~ 100-220. GD divides a level into sections along **x**, and
/// `LevelEditorLayer::m_objectVector` -- a pre-sized table (10000 slots, all
/// null, on a level with a handful of objects) -- looks like that
/// section-indexed structure. Removing an object whose section was never
/// allocated would index outside it.
///
/// Which axis is responsible is not fully isolated: the failing cases varied x
/// in one run and y in the other, while the known-good case used modest values
/// on both. So the safe rule is "modest on both axes" until someone pins it
/// down. That is a real constraint for `create_objects`, which should validate
/// coordinates rather than accept any number.
///
/// Because the objects are no longer set apart by position, they are identified
/// by the unique IDs returned from creation, not by a region filter.
constexpr double kProbeLevelY = 750.0;
constexpr double kProbeX = 900.0;

} // namespace

arc::Future<> runHandlerVerification() {
    log::info("handler verification: starting");

    Verifier verifier;

    // Any transaction left open by an earlier probe would make ours report
    // TRANSACTION_ACTIVE.
    (void) co_await onMain([] {
        editor::TransactionManager::get().abortOpen("handler verification");
        return 0;
    });

    auto const initialCount = co_await objectCount();
    if (initialCount < 0) {
        log::error("handler verification: no editor open, cannot run");
        co_return;
    }

    auto openedResult = co_await onMain([] {
        auto result = editor::TransactionManager::get().begin("handler verification");
        return result ? result.unwrap() : std::string {};
    });
    // `waitForMainThread` reports a failed dispatch as a disengaged optional.
    auto const opened = openedResult.value_or(std::string {});
    verifier.check(!opened.empty(), "begin transaction");
    if (opened.empty()) {
        co_return;
    }

    // -- does removing a PRE-EXISTING object work across frames? -------------
    //
    // Every crash so far involved removing an object this mod had created via
    // `createObject`. Never tested: removing an object that was already in the
    // level. That distinction decides whether `createObject` produces objects the
    // editor cannot safely un-register later, or whether cross-frame removal is
    // unsafe for any object at all.
    //
    // Runs first, before this harness creates anything, so the target is
    // definitely a pre-existing object.
    {
        auto queryPayload = matjson::Value::object();
        auto filters = matjson::Value::object();
        auto range = matjson::Value::array();
        range.push(-100000.0);
        range.push(100000.0);
        filters["x_range"] = std::move(range);
        queryPayload["filters"] = std::move(filters);
        queryPayload["limit"] = 1;

        auto found = co_await submit("query_objects", std::move(queryPayload));
        std::int32_t existingId = 0;
        if (found.ok && found.payload.contains("objects")) {
            for (auto const& record : found.payload["objects"]) {
                if (auto id = record["object_id"].asInt(); id.ok()) {
                    existingId = static_cast<std::int32_t>(id.unwrap());
                }
            }
        }

        if (existingId == 0) {
            log::warn("handler verification: no pre-existing object to try removing");
        } else {
            log::info("handler verification: trying to remove PRE-EXISTING object {}", existingId);
            auto deletePayload = matjson::Value::object();
            auto targets = matjson::Value::array();
            targets.push(existingId);
            deletePayload["targets"] = std::move(targets);

            auto removed = co_await submit("delete_objects", std::move(deletePayload));
            log::info(
                "REMOVE-EXISTING-RESULT: id {} -> ok={} {}",
                existingId, removed.ok, removed.payload.dump(0)
            );
            verifier.check(
                removed.ok, "removing a pre-existing object across frames",
                removed.payload.dump(0)
            );
        }
    }

    // -- create_objects -----------------------------------------------------
    //
    // Coordinates are ordinary (see the note on kProbeX): extreme values crash
    // removal. The objects are tracked by their returned unique IDs instead of by
    // position.
    auto createPayload = matjson::Value::object();
    auto createArray = matjson::Value::array();
    createArray.push(objectEntry(1, kProbeX, kProbeLevelY));
    createArray.push(objectEntry(1, kProbeX + 30.0, kProbeLevelY));
    createArray.push(objectEntry(1, kProbeX + 60.0, kProbeLevelY));
    createPayload["objects"] = std::move(createArray);

    auto created = co_await submit("create_objects", std::move(createPayload));
    verifier.check(created.ok, "create_objects succeeds", created.payload.dump(0));
    verifier.checkEqual(
        created.payload["count"].asInt().unwrapOr(-1), 3, "create_objects count"
    );

    auto const afterCreate = co_await objectCount();
    verifier.checkEqual(afterCreate - initialCount, 3, "level grew by 3 objects");

    // IDs come back from the handler, which is also what the sidecar would use.
    std::vector<std::int32_t> ids;
    if (created.payload.contains("created")) {
        for (auto const& entry : created.payload["created"]) {
            if (auto id = entry.asInt(); id.ok()) {
                ids.push_back(static_cast<std::int32_t>(id.unwrap()));
            }
        }
    }
    verifier.checkEqual(static_cast<long long>(ids.size()), 3, "three ids returned");

    if (ids.size() == 3) {
        // -- the level/cocos y conversion -----------------------------------
        // Created at level y 8000, so the wire read must return 8000, not the
        // cocos position. This is the check that the conversion is applied in
        // both directions.
        auto const y = co_await readNumber(ids[0], "y");
        verifier.check(
            y.has_value() && std::abs(*y - kProbeLevelY) < 0.01,
            "y round trips in level coordinates",
            fmt::format("expected {}, got {}", kProbeLevelY, y.has_value() ? *y : -1.0)
        );

        auto const x = co_await readNumber(ids[1], "x");
        verifier.check(
            x.has_value() && std::abs(*x - (kProbeX + 30.0)) < 0.01,
            "x read back",
            fmt::format("expected {}, got {}", kProbeX + 30.0, x.has_value() ? *x : -1.0)
        );

        // -- modify_objects -------------------------------------------------

        auto modifyPayload = matjson::Value::object();
        auto targets = matjson::Value::array();
        targets.push(ids[0]);
        modifyPayload["targets"] = std::move(targets);

        auto set = matjson::Value::object();
        set["x"] = 250.0;
        set["scale_x"] = 1.5;
        set["flip_x"] = true;
        modifyPayload["set"] = std::move(set);

        auto delta = matjson::Value::object();
        delta["y"] = 30.0;
        modifyPayload["delta"] = std::move(delta);

        auto modified = co_await submit("modify_objects", std::move(modifyPayload));
        verifier.check(modified.ok, "modify_objects succeeds", modified.payload.dump(0));
        verifier.checkEqual(
            modified.payload["modified_count"].asInt().unwrapOr(-1), 1, "one object modified"
        );

        auto const newX = co_await readNumber(ids[0], "x");
        verifier.check(
            newX.has_value() && std::abs(*newX - 250.0) < 0.01,
            "absolute set applied",
            fmt::format("expected 250, got {}", newX.has_value() ? *newX : -1.0)
        );

        // A delta must compose with the *current* value, so 8000 + 30.
        auto const newY = co_await readNumber(ids[0], "y");
        verifier.check(
            newY.has_value() && std::abs(*newY - (kProbeLevelY + 30.0)) < 0.01,
            "delta applied relative to current y",
            fmt::format("expected {}, got {}", kProbeLevelY + 30.0, newY.has_value() ? *newY : -1.0)
        );

        // scale_x is the property whose setter was measured as ineffective, so
        // this confirms the field route is the one the handler uses.
        auto const scale = co_await readNumber(ids[0], "scale_x");
        verifier.check(
            scale.has_value() && std::abs(*scale - 1.5) < 0.01,
            "scale_x set through the field route",
            fmt::format("expected 1.5, got {}", scale.has_value() ? *scale : -1.0)
        );

        // -- query_objects and summarize_region -----------------------------

        // Queried by unique ID rather than by range, because the modify checks
        // above deliberately move one of the objects (x -> 250). A range filter
        // would then miss it and report a false failure.
        auto queryPayload = matjson::Value::object();
        auto filters = matjson::Value::object();
        auto wantedIds = matjson::Value::array();
        for (auto const id : ids) {
            wantedIds.push(id);
        }
        filters["ids"] = std::move(wantedIds);
        queryPayload["filters"] = std::move(filters);

        auto queried = co_await submit("query_objects", std::move(queryPayload));
        verifier.check(queried.ok, "query_objects succeeds", queried.payload.dump(0));
        verifier.checkEqual(
            queried.payload["total_matches"].asInt().unwrapOr(-1), 3,
            "query found the three created objects"
        );

        // `summarize_region` has no id filter, so this uses a narrow box around
        // the creation positions. The assertion is `>= 3` because the level's own
        // objects could fall inside it; the exact count is logged for inspection.
        auto regionPayload = matjson::Value::object();
        auto regionX = matjson::Value::array();
        regionX.push(kProbeX - 60.0);
        regionX.push(kProbeX + 90.0);
        regionPayload["x_range"] = std::move(regionX);
        auto regionY = matjson::Value::array();
        regionY.push(kProbeLevelY - 50.0);
        regionY.push(kProbeLevelY + 50.0);
        regionPayload["y_range"] = std::move(regionY);

        auto summarized = co_await submit("summarize_region", std::move(regionPayload));
        verifier.check(summarized.ok, "summarize_region succeeds", summarized.payload.dump(0));
        auto const regionTotal = summarized.payload["total"].asInt().unwrapOr(-1);
        verifier.check(
            regionTotal >= 2,
            "region contains at least the two objects still at the probe position",
            fmt::format("total was {}", regionTotal)
        );

        // -- delete_objects -------------------------------------------------
        //
        // Gated (see kTestDeleteObjects). The object to delete is still
        // identified so the later ordering is unchanged, but no removal happens.
        if (kTestDeleteObjects) {
            auto deletePayload = matjson::Value::object();
            auto deleteTargets = matjson::Value::array();
            deleteTargets.push(ids[2]);
            deletePayload["targets"] = std::move(deleteTargets);

            auto deleted = co_await submit("delete_objects", std::move(deletePayload));
            verifier.check(deleted.ok, "delete_objects succeeds", deleted.payload.dump(0));
            verifier.checkEqual(
                deleted.payload["deleted_count"].asInt().unwrapOr(-1), 1, "one object deleted"
            );

            auto const afterDelete = co_await objectCount();
            verifier.checkEqual(afterDelete - initialCount, 2, "level is two objects larger");
        } else {
            log::info("handler verification: delete_objects step SKIPPED (see kTestDeleteObjects)");
        }

        // -- set_selection --------------------------------------------------

        auto selectPayload = matjson::Value::object();
        auto selectTargets = matjson::Value::array();
        selectTargets.push(ids[0]);
        selectTargets.push(ids[1]);
        selectPayload["targets"] = std::move(selectTargets);

        auto selected = co_await submit("set_selection", std::move(selectPayload));
        verifier.check(selected.ok, "set_selection succeeds", selected.payload.dump(0));
        verifier.checkEqual(
            selected.payload["selected_count"].asInt().unwrapOr(-1), 2, "two objects selected"
        );
        // Clear the selection so it does not affect later reads.
        (void) co_await onMain([] {
            if (auto* editor = LevelEditorLayer::get(); editor != nullptr
                && editor->m_editorUI != nullptr) {
                editor->m_editorUI->deselectAll();
            }
            return 0;
        });
    }

    // -- validation is whole-request and off the main thread -----------------

    auto badProperty = matjson::Value::object();
    auto badArray = matjson::Value::array();
    auto badEntry = objectEntry(1, 0.0, 0.0);
    auto badProps = matjson::Value::object();
    badProps["not_a_property"] = 1;
    badEntry["properties"] = std::move(badProps);
    badArray.push(std::move(badEntry));
    badProperty["objects"] = std::move(badArray);

    auto rejected = co_await submit("create_objects", std::move(badProperty));
    verifier.check(
        !rejected.ok && rejected.payload["code"].asString().unwrapOr("") == "UNSUPPORTED_PROPERTY",
        "unknown property rejected with UNSUPPORTED_PROPERTY",
        rejected.payload.dump(0)
    );

    auto emptyTargets = matjson::Value::object();
    emptyTargets["targets"] = matjson::Value::array();
    auto emptyRejected = co_await submit("delete_objects", std::move(emptyTargets));
    verifier.check(
        !emptyRejected.ok, "empty targets rejected", emptyRejected.payload.dump(0)
    );

    auto missingTarget = matjson::Value::object();
    auto missingTargets = matjson::Value::array();
    missingTargets.push(999999);
    missingTarget["targets"] = std::move(missingTargets);
    auto missingDelete = co_await submit("delete_objects", std::move(missingTarget));
    verifier.check(
        !missingDelete.ok
            && missingDelete.payload["code"].asString().unwrapOr("") == "OBJECT_NOT_FOUND",
        "deleting a nonexistent id reports OBJECT_NOT_FOUND",
        missingDelete.payload.dump(0)
    );

    auto badDelta = matjson::Value::object();
    auto badDeltaTargets = matjson::Value::array();
    badDeltaTargets.push(1);
    badDelta["targets"] = std::move(badDeltaTargets);
    auto badDeltaValues = matjson::Value::object();
    badDeltaValues["groups"] = 3;
    badDelta["delta"] = std::move(badDeltaValues);
    auto badDeltaResult = co_await submit("modify_objects", std::move(badDelta));
    verifier.check(
        !badDeltaResult.ok
            && badDeltaResult.payload["code"].asString().unwrapOr("") == "UNSUPPORTED_PROPERTY",
        "delta on a non-delta property is refused",
        badDeltaResult.payload.dump(0)
    );

    auto badRange = matjson::Value::object();
    auto badFilters = matjson::Value::object();
    auto shortRange = matjson::Value::array();
    shortRange.push(1.0);
    badFilters["x_range"] = std::move(shortRange);
    badRange["filters"] = std::move(badFilters);
    auto badRangeResult = co_await submit("query_objects", std::move(badRange));
    verifier.check(
        !badRangeResult.ok, "malformed range rejected", badRangeResult.payload.dump(0)
    );

    // -- revert the whole turn ---------------------------------------------

    auto endedResult = co_await onMain([&opened] {
        auto result = editor::TransactionManager::get().end(opened);
        return result ? result.unwrap().dump(0) : std::string {};
    });
    auto const ended = endedResult.value_or(std::string {});
    verifier.check(!ended.empty(), "end transaction", ended);

    auto revertedResult = co_await onMain([&opened] {
        auto result = editor::TransactionManager::get().revert(opened);
        return result ? result.unwrap().dump(0) : std::string {};
    });
    auto const reverted = revertedResult.value_or(std::string {});
    verifier.check(!reverted.empty(), "revert transaction", reverted);

    auto const afterRevert = co_await objectCount();
    verifier.checkEqual(
        afterRevert - initialCount, 0, "level restored to its original object count"
    );

    log::info(
        "handler verification: {} passed, {} failed", verifier.passed, verifier.failed
    );
    if (verifier.failed == 0) {
        log::info("HANDLER-VERIFY: ALL CHECKS PASSED");
    } else {
        log::error("HANDLER-VERIFY: {} CHECKS FAILED", verifier.failed);
    }
}

} // namespace gdai::exec
