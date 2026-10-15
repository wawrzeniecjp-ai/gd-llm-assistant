#pragma once

#include <matjson.hpp>

#include <memory>
#include <string>
#include <string_view>

#include "../exec/job_queue.hpp"

namespace gdai::exec {

/// Builds a resumable job for a protocol request (SPEC.md 7.5).
///
/// Validation happens here, before the job is queued: SPEC.md 6.1.4 requires a
/// request to be rejected **whole** rather than half-applied, so a malformed
/// payload never reaches the main thread. A validation failure is returned as a
/// job that completes immediately with the error, which keeps the caller's flow
/// uniform -- one code path submits and awaits, whether or not the payload was
/// acceptable.
///
/// Returns nullptr for a request type the mod does not implement, which the
/// caller reports as `INVALID_REQUEST` rather than leaving to time out.
std::unique_ptr<Job> makeJob(std::string_view type, matjson::Value const& payload);

/// Request types this mod implements. Used by the bridge to answer unknown types
/// quickly instead of queueing them.
bool isImplementedRequest(std::string_view type);

/// A job that has already failed validation and simply reports the error.
///
/// Exists so that `makeJob` has one return type and the caller never has to
/// distinguish "invalid payload" from "valid payload" structurally.
std::unique_ptr<Job> makeErrorJob(std::string errorObject);

} // namespace gdai::exec
