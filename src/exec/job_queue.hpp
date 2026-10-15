#pragma once

#include <arc/future/Future.hpp>
#include <arc/sync/oneshot.hpp>

#include <matjson.hpp>

#include <chrono>
#include <cstddef>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace gdai::exec {

/// The result of a completed job.
///
/// `payload` is either the protocol `result` object or, when `ok` is false, an
/// `error` object shaped as {code, message, details?}. Keeping both in one type
/// avoids a second error channel for "the job ran but the operation failed",
/// which is a normal outcome rather than an exception (SPEC.md 11).
struct JobOutcome {
    bool ok = false;
    matjson::Value payload;

    static JobOutcome success(matjson::Value result);
    static JobOutcome failure(std::string_view code, std::string_view message);
};

/// A resumable unit of main-thread work (SPEC.md 6.1.4).
///
/// A request is not executed in one callback. `step()` performs one slice of
/// work -- one object, typically -- and the queue calls it again on later frames
/// until it reports completion. That is what keeps a 1000-object request from
/// stalling a frame (A1): a single `waitForMainThread` call around the whole
/// batch would block regardless of the frame budget.
///
/// Implementations run **on the main thread only** and may touch Cocos2d and GD
/// freely. They must not block, and must not hold a `GameObject*` across a
/// `step()` boundary unless the object is re-resolved by unique ID -- the editor
/// can be modified between slices by the user or an undo (SPEC.md 6.1.3 rule 5).
class Job {
public:
    virtual ~Job() = default;

    /// Performs one slice. Returns true when the job is complete.
    [[nodiscard]] virtual bool step() = 0;

    /// The job's result. Called exactly once, after `step()` returns true.
    [[nodiscard]] virtual JobOutcome takeOutcome() = 0;

    /// The protocol request type, for logging and progress events.
    [[nodiscard]] virtual std::string_view type() const = 0;
};

/// Serialises resumable editor work onto the main thread (SPEC.md 6.1.4).
///
/// The network side validates a request, submits it here, and awaits completion.
/// The main thread drains the queue from a per-frame hook, honouring the
/// configured frame budget. Nothing on the network side ever touches the editor.
class JobQueue {
public:
    static JobQueue& get();

    /// Submits validated work and awaits its outcome.
    ///
    /// Safe to call from the async runtime. Returns immediately with a failure
    /// outcome if the queue is full, rather than growing without bound
    /// (`queued_jobs`, SPEC.md 6.1.4).
    arc::Future<JobOutcome> submit(std::unique_ptr<Job> job);

    /// Runs queued work within `budgetMs`, resuming an in-progress job first.
    ///
    /// Called from the per-frame main-thread hook. Uses a monotonic clock, so a
    /// job that overruns cannot extend the budget on subsequent frames.
    void drain(double budgetMs);

    /// Jobs waiting, excluding one currently in progress.
    [[nodiscard]] std::size_t pendingCount() const;

    /// Hard cap on queued jobs, from SPEC.md 6.1.4.
    static constexpr std::size_t kMaxQueuedJobs = 16;

private:
    JobQueue() = default;

    struct Pending {
        std::unique_ptr<Job> job;
        /// Delivers the outcome to the awaiting caller. Dropping the receiver
        /// (a disconnect or cancellation) makes `send` fail, which is how a
        /// cancelled job is detected at a slice boundary.
        arc::oneshot::Sender<JobOutcome> completion;
    };

    mutable std::mutex m_mutex;
    std::vector<Pending> m_waiting;
    /// The job spanning frames, if any. Held out of `m_waiting` so it resumes
    /// before newly submitted work rather than being re-queued behind it.
    ///
    /// Touched only by `drain`, i.e. only on the main thread; `submit` never
    /// reads it, which is why it needs no lock of its own.
    std::optional<Pending> m_current;
};

} // namespace gdai::exec
