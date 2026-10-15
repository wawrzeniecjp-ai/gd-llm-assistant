#pragma once

#include <arc/future/Future.hpp>

namespace gdai::exec {

/// DEV: end-to-end verification of the editor request handlers.
///
/// The handlers are the only code in this project with no measurement behind it:
/// the property layer, the job queue and the transaction manager were each
/// probe-verified, but `create_objects` and friends have only been type-checked.
/// This closes that gap.
///
/// Why it is a coroutine rather than a main-thread probe: `JobQueue::submit()`
/// awaits a oneshot completed by `JobQueue::drain()`, and `drain()` runs from the
/// editor's per-frame update on the main thread. A test running *on* the main
/// thread would block the very thread that has to drain it, and deadlock. So it
/// runs on the async runtime and lets frames pass -- which is precisely how the
/// real network path submits work, so the test exercises the production flow.
///
/// It covers: job slicing, whole-request validation, transaction recording,
/// revert across creates/modifies/deletes, the level/cocos y conversion, and the
/// error codes each handler reports.
///
/// **Mutates the level** (creates, modifies and deletes objects) but reverts
/// everything through the transaction manager, so the level is restored unless a
/// step fails partway.
arc::Future<> runHandlerVerification();

} // namespace gdai::exec
