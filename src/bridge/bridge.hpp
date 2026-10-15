#pragma once

#include <Geode/utils/async.hpp>

#include <atomic>
#include <string>

#include "../net/websocket.hpp"

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

    /// True while a session is running or a reconnect is pending.
    bool isActive() const {
        return m_active.load();
    }

private:
    arc::Future<> run(std::string url);
    arc::Future<> session(net::WebSocket socket, std::string token);
    arc::Future<> handleFrame(std::string const& frame, net::WebSocket& socket);

    std::atomic<bool> m_active { false };
    std::atomic<bool> m_stopRequested { false };
    /// Set by the game's Exiting event, so the loop can stop without hopping to
    /// a main thread that may already be tearing down.
    std::atomic<bool> m_gameExiting { false };
};

} // namespace gdai::bridge
