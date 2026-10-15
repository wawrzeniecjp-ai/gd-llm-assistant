#include "job_queue.hpp"

#include <Geode/Geode.hpp>

#include <utility>

using namespace geode::prelude;

namespace gdai::exec {

JobOutcome JobOutcome::success(matjson::Value result) {
    return JobOutcome { true, std::move(result) };
}

JobOutcome JobOutcome::failure(std::string_view code, std::string_view message) {
    auto error = matjson::Value::object();
    error["code"] = std::string(code);
    error["message"] = std::string(message);
    return JobOutcome { false, std::move(error) };
}

JobQueue& JobQueue::get() {
    static JobQueue instance;
    return instance;
}

arc::Future<JobOutcome> JobQueue::submit(std::unique_ptr<Job> job) {
    if (job == nullptr) {
        co_return JobOutcome::failure("INTERNAL_ERROR", "submitted a null job");
    }

    auto [sender, receiver] = arc::oneshot::channel<JobOutcome>();

    {
        // A plain mutex, never held across a suspension point: `submit` only
        // appends, and `drain` only moves a job in or out.
        std::lock_guard lock(m_mutex);
        if (m_waiting.size() >= kMaxQueuedJobs) {
            log::warn("job queue full ({}); rejecting {}", kMaxQueuedJobs, job->type());
            co_return JobOutcome::failure(
                "LIMIT_EXCEEDED", fmt::format("more than {} jobs are queued", kMaxQueuedJobs)
            );
        }
        m_waiting.push_back(Pending { std::move(job), std::move(sender) });
    }

    // Resolves when the main thread completes the job, or when this future is
    // dropped (disconnect/cancel), which closes the channel. `recv()` returns
    // the awaitable, so a bare `co_await receiver` would not compile.
    auto outcome = co_await receiver.recv();
    co_return std::move(outcome).unwrap();
}

void JobQueue::drain(double budgetMs) {
    auto const start = std::chrono::steady_clock::now();
    auto const elapsedMs = [&start] {
        return std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - start
        ).count();
    };

    while (elapsedMs() < budgetMs) {
        if (!m_current.has_value()) {
            std::lock_guard lock(m_mutex);
            if (m_waiting.empty()) {
                return;
            }
            // Take the oldest; FIFO keeps request ordering predictable.
            m_current = std::move(m_waiting.front());
            m_waiting.erase(m_waiting.begin());
        }

        auto& pending = *m_current;
        auto const finished = pending.job->step();

        if (!finished) {
            // Budget is re-checked at the top of the loop, so an overrunning
            // slice yields the frame and resumes next time.
            continue;
        }

        auto outcome = pending.job->takeOutcome();
        auto const type = std::string(pending.job->type());

        // `send` fails if the caller went away, which is a normal outcome on
        // disconnect rather than an error worth logging at warn level.
        if (auto sent = pending.completion.send(std::move(outcome)); !sent) {
            log::info("job '{}' completed after the caller went away", type);
        }
        m_current.reset();
    }
}

std::size_t JobQueue::pendingCount() const {
    std::lock_guard lock(m_mutex);
    return m_waiting.size();
}

} // namespace gdai::exec
