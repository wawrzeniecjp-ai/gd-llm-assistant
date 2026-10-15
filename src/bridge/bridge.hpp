#pragma once

#include <Geode/utils/async.hpp>

#include <atomic>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include "../net/websocket.hpp"
#include "../ui/chat_panel.hpp"

namespace gdai::bridge {

/// Owns the single WebSocket connection to the sidecar (SPEC.md 7.8, D1).
///
/// Phase 1 scope: connect when the editor opens, `hello` handshake, request
/// dispatch for `ping` and `get_editor_state`, `editor_closed` on close, and
/// reconnect with exponential backoff. The main-thread job queue, transactions
/// and the remaining request set are Phase 2 and 4 work.
///
/// Threading: the run loop lives on the Arc runtime. Editor reads hop to the
/// main thread with `async::waitForMainThread`, and the socket is touched only
/// by the runtime thread (SPEC.md 6.1.3).
class Bridge {
public:
    static Bridge& get();

    /// Connect and start serving. Idempotent, so the editor-open hook may call
    /// it freely.
    void start();

    /// Ask the session to finish: it sends `editor_closed` and closes the
    /// socket, then stops retrying (SPEC.md 7.8 step 6). Returns immediately;
    /// safe to call from the main thread.
    void stop();

    /// Called from the game's `Exiting` event.
    ///
    /// This exists because `LevelEditorLayer::onExit` is **not declared in the
    /// Geode bindings** (unlike `PlayLayer` and many other layers), so a
    /// `$modify` hook on it silently never fires. Relying on it left the session
    /// loop polling forever, and Geode's async-runtime shutdown then blocked on
    /// that task -- a 5-10 second hang on game exit.
    void onGameExiting();

    /// Sends a `user_message` event (SPEC.md 7.4). Safe to call from the main
    /// thread.
    void sendUserMessage(std::string text);

    /// Sends a `cancel_turn` event. Safe to call from the main thread.
    void sendCancelTurn();

    /// True while a session is running or a reconnect is pending.
    bool isActive() const {
        return m_active.load();
    }

private:
    /// Per-session state, shared with that session's watchdog.
    ///
    /// Not a member of `Bridge`: a session ends, the reconnect loop starts the
    /// next one immediately, and the previous watchdog may still be mid-sleep.
    /// The two must not be able to see each other, or a stale watchdog would
    /// keep running against a dead socket -- flushing frames into it, forever,
    /// one leaked task per reconnect.
    struct SessionState {
        std::atomic<bool> running { true };
        /// Set by whichever task performs the teardown, so the other does not
        /// repeat it on a socket that is already shut.
        std::atomic<bool> tornDown { false };
    };

    arc::Future<> run(std::string url);
    arc::Future<> session(std::shared_ptr<net::WebSocket> socket, std::string token);
    arc::Future<> handleFrame(std::string const& frame, net::WebSocket& socket);

    /// Ends a session from outside its receive loop.
    ///
    /// Runs alongside `session` and does the two things the receive loop cannot
    /// do for itself while it is blocked: flush queued outbound frames, and
    /// notice that the session should end. On the latter it performs the graceful
    /// teardown (`editor_closed`, then shutdown) and returns, which unblocks the
    /// receive.
    ///
    /// This exists because the receive loop must not be interrupted (see
    /// `net::WebSocket`), so it can no longer be the thing that polls for the
    /// editor closing.
    arc::Future<> watchSession(std::shared_ptr<net::WebSocket> socket, std::shared_ptr<SessionState> state);

    /// Queues a frame for the session loop to send.
    ///
    /// The socket is touched by the runtime thread only (SPEC.md 6.1.3), but the
    /// panel that wants to send lives on the main thread. Rather than share the
    /// socket, outbound frames go through this queue and the session loop drains
    /// it. The current turn id is kept here too, since it is read from the main
    /// thread when the user presses Stop.
    void enqueueFrame(std::string frame);

    /// Handles an event pushed by the sidecar, which carries a null id and
    /// expects no reply.
    void handleEvent(std::string const& type, matjson::Value const& payload);

    /// Sends every frame queued by `enqueueFrame`. Runtime thread only.
    arc::Future<> flushOutbound(net::WebSocket& socket);

    /// Updates the panel's connection dot, and adds a system line when the state
    /// actually changed.
    ///
    /// The de-duplication is the point: the connection loop retries on a backoff
    /// that reaches 30 s, and posting unconditionally would fill the transcript
    /// with one "Reconnecting" per retry.
    void setConnectionStatus(ui::Status status, std::string systemText);

    /// The last status posted, so `setConnectionStatus` can spot a repeat.
    /// Written from the runtime thread, read from the runtime thread.
    std::optional<ui::Status> m_lastStatus;

    /// Applies the `hello` result: connection state for the panel, and the
    /// transcript the sidecar replays.
    void applyHelloResult(matjson::Value const& payload);

    std::atomic<bool> m_active { false };
    std::atomic<bool> m_stopRequested { false };
    /// Set by the game's Exiting event, so the loop can stop without hopping to
    /// a main thread that may already be tearing down.
    std::atomic<bool> m_gameExiting { false };

    /// Outbound frames, drained by the session loop. Guarded because `send*` is
    /// called from the main thread while the loop runs on the runtime.
    std::mutex m_outboundMutex;
    std::deque<std::string> m_outbound;

    /// The turn currently running, or empty. Read from the main thread by
    /// `sendCancelTurn`, written by the session loop.
    ///
    /// A `std::string` guarded by a mutex rather than a lock-free type: it is
    /// touched a handful of times per turn, not per frame, so contention is not
    /// a consideration and correctness is easier to see.
    std::mutex m_turnMutex;
    std::string m_activeTurnId;

    /// Monotonic counter for the mod's own request and message ids.
    std::atomic<uint64_t> m_nextId { 1 };
};

} // namespace gdai::bridge
