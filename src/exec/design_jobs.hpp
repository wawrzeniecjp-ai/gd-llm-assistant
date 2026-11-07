#pragma once

#include <matjson.hpp>

#include <memory>
#include <string_view>

#include "job_queue.hpp"

namespace gdai::exec {

/// Requests added for the level design layer (LEVEL-DESIGN-SPEC.md 4.2, 4.3).
///
/// Kept apart from `editor_jobs.cpp` because they are read-only and serve a
/// different consumer: the sidecar's design package, which reads the level whole
/// rather than a few objects at a time.
///
/// - `get_level_objects`: every object as its save string, paged, guarded by the
///   level revision so a page boundary cannot silently straddle an edit.
/// - `describe_object_types`: each object type instantiated **off-scene** and
///   described -- gameplay type, hitbox, radius -- which is where the design
///   layer's role table comes from.
bool isDesignRequest(std::string_view type);

/// Builds the job, or an already-failed job when the payload is invalid.
/// Returns nullptr for a type `isDesignRequest` does not accept.
std::unique_ptr<Job> makeDesignJob(std::string_view type, matjson::Value const& payload);

} // namespace gdai::exec
