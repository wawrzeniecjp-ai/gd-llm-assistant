#pragma once

#include "job_queue.hpp"

#include <matjson.hpp>

#include <memory>

namespace gdai::exec {

/// `run_replay` (LEVEL-DESIGN-SPEC.md 7.2): play the open level in the editor's own
/// playtest with a scripted input sequence, at physics-tick granularity, and report the
/// player's trace and how the run ended.
///
/// **This is also spike S-L3.** What the bindings cannot tell us -- whether
/// `GJBaseGameLayer::processCommands` runs once per physics tick during the editor's
/// playtest, whether `handleButton` called before it takes effect on that tick, how
/// death shows up -- is measured, and the result carries a `diagnostics` object with
/// the raw counts so the answer can be read off a real run rather than assumed.
///
/// Shape of a run: the job starts the playtest (`EditorUI::onPlaytest`), a hook on
/// `processCommands` counts ticks, injects the scripted presses and releases just
/// before the tick they belong to, and samples the player just after it. The job polls
/// once per frame and stops the playtest (`EditorUI::onStopPlaytest`) once the run has
/// ended: death, `max_ticks`, or the user taking over (a press that is not ours, or
/// leaving playtest), which is `CANCELLED`.
std::unique_ptr<Job> makeReplayJob(matjson::Value const& payload);

} // namespace gdai::exec
