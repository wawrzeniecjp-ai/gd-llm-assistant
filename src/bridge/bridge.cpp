#include "bridge.hpp"

#include <Geode/binding/LevelEditorLayer.hpp>
#include <Geode/loader/Mod.hpp>
#include <Geode/utils/async.hpp>

#include <arc/prelude.hpp>

#include <fmt/format.h>

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <iterator>

#include "../editor/camera.hpp"
#include "../editor/color_channel.hpp"
#include "../editor/editor_state.hpp"
#include "../editor/level_settings.hpp"
#include "../editor/object_catalog.hpp"
#include "../editor/screenshot.hpp"
#include "../editor/transaction.hpp"
#include "../editor/level_revision.hpp"
#include "../exec/design_jobs.hpp"
#include "../exec/editor_jobs.hpp"
#include "../exec/handler_verification.hpp"
#include "../exec/job_queue.hpp"
#include "../ui/panel_host.hpp"

using namespace geode::prelude;

namespace gdai::bridge {
namespace {

/// Developer probes run at most once per game session.
///
/// They are diagnostic tools, not runtime code, and two of them mutate the level
/// (one clears the undo stack). Re-running them on every editor open would
/// repeatedly disturb the level and the user's undo history for no benefit, so
/// the first editor open triggers them and later ones do not. Restart the game
/// to re-run.
bool g_probesRan = false;

void runDeveloperProbesOnce() {
    if (g_probesRan) {
        return;
    }
    g_probesRan = true;

    log::info("developer probes: running (restart the game to run them again)");
    editor::dumpSaveStringSamples();
    editor::runDifferentialProbe();
    editor::runUndoProbe();
    editor::runUndoShapeProbe();
    editor::runTransactionProbe();
    log::info("developer probes: done");

    // The handler verification is a coroutine that submits jobs and awaits the
    // per-frame drain, so it cannot run on the main thread (see its header).
    // Spawned after the synchronous probes so the two do not interleave.
    async::spawn(exec::runHandlerVerification);
}

/// How often the watchdog ticks: checks for a queued frame, and for the editor
/// closing or the game exiting.
///
/// This is the latency ceiling on sending a queued frame, so it is short enough
/// that pressing Send feels immediate, and long enough that a handful of
/// wake-ups a second is negligible. It is *not* a timeout applied to the
/// receive -- the receive must never be cancelled, which is the whole reason
/// this is a separate task (see `net::WebSocket`).
constexpr auto kWachdogInterval = asp::time::Duration::fromMillis(150);

/// Every this many watchdog ticks (about 3 s), send a ping to check the sidecar
/// is still there. A dead peer does not always wake the pending receive on
/// Windows, but a send to it fails, and that is what ends the session so the
/// reconnect loop can run (see `net::WebSocket::sendPing`).
constexpr int kLivenessPingTicks = 20;

/// Correlation id of the `hello` request. The mod issues exactly one request, so
/// the only `response` it should ever receive carries this id.
constexpr char const* kHelloRequestId = "m-1";

/// Reconnect backoff: 1 s doubling to a 30 s cap (SPEC.md 7.8 step 5).
constexpr int kInitialBackoffSecs = 1;
constexpr int kMaxBackoffSecs = 30;

/// `%LOCALAPPDATA%\gd-ai-assistant\token` (SPEC.md 7.2). A handshake secret,
/// not the LLM API key.
std::filesystem::path tokenFilePath() {
    auto const localAppData = geode::utils::getEnvironmentVariable("LOCALAPPDATA");
    if (!localAppData.empty()) {
        return std::filesystem::path(localAppData) / "gd-ai-assistant" / "token";
    }
    return std::filesystem::path("gd-ai-assistant") / "token";
}

std::string readTokenFile(std::filesystem::path const& path) {
    std::ifstream file(path, std::ios::binary);
    // Bind the iterators to named locals first: writing
    // `std::string(first, last)` directly would parse as a function declaration.
    auto const first = std::istreambuf_iterator<char>(file);
    auto const last = std::istreambuf_iterator<char>();
    std::string contents(first, last);
    while (!contents.empty() && std::isspace(static_cast<unsigned char>(contents.back()))) {
        contents.pop_back();
    }
    return contents;
}

constexpr char const* kDefaultUrl = "ws://127.0.0.1:18765";

/// The default before 0.2f. Geode saves every setting's value, so an install that never
/// touched the URL keeps the old default after the change; that value is moved to the
/// new default once. (8765 kept colliding with other local dev servers.)
constexpr char const* kOldDefaultUrl = "ws://127.0.0.1:8765";

std::string configuredUrl() {
    auto url = Mod::get()->getSettingValue<std::string>("sidecar-url");
    if (url == kOldDefaultUrl) {
        Mod::get()->setSettingValue<std::string>("sidecar-url", kDefaultUrl);
        log::info("sidecar-url: moved the old default {} to {}", kOldDefaultUrl, kDefaultUrl);
        url = kDefaultUrl;
    }
    return url.empty() ? std::string(kDefaultUrl) : url;
}

/// The session key for a level (SPEC.md 6.2.5).
///
/// In practice this is the **level name**, because the stable editor ID is no
/// longer available: it came from `cvolton.level-id-api`, which was dropped
/// (SPEC.md 0.1) after Geode v6's event rework stopped its headers compiling.
///
/// The name is a weaker key -- it collides across levels and changes on rename --
/// but that is the caveat SPEC.md 15 already records as open, and it was the
/// documented default before the dependency existed. The `levelId` branch is kept
/// so restoring the dependency later needs no change here.
///
/// Shared by `buildHello` and the log line that reports it, so the log cannot
/// claim a key different from the one actually sent. It previously reported
/// "(unnamed)" on every session, because it tested `levelId` -- which is now
/// always 0 -- while the frame correctly carried the level name.
std::string sessionKey(editor::Snapshot const& snapshot) {
    if (snapshot.levelId != 0) {
        return fmt::format("editor-{}", snapshot.levelId);
    }
    if (!snapshot.levelName.empty()) {
        return snapshot.levelName;
    }
    return "unknown";
}

/// Builds the `hello` frame (SPEC.md 7.2).
///
/// Carries only the opaque session key and client versions. The level **name**
/// and object count are deliberately absent: sending them here would push level
/// content to the sidecar before the user has typed anything, which contradicts
/// SPEC.md 12 ("nothing is sent until the user submits a message") and SPEC.md
/// D4 (query on demand, no mirror). Both arrive via `get_editor_state` when the
/// sidecar actually needs them.
std::string buildHello(std::string_view token, editor::Snapshot const& snapshot) {    auto hello = matjson::Value::object();
    hello["type"] = "hello";
    hello["id"] = kHelloRequestId;

    auto payload = matjson::Value::object();
    payload["protocol_version"] = 1;
    payload["token"] = std::string(token);

    auto client = matjson::Value::object();
    client["mod_version"] = Mod::get()->getVersion().toNonVString();
    client["gd_version"] = Loader::get()->getGameVersion();
    client["geode_version"] = Loader::get()->getVersion().toNonVString();
    payload["client"] = std::move(client);

    // Session key (SPEC.md 6.2.5). See `sessionKey` for why this is the level name
    // in practice rather than the stable editor ID.
    auto level = matjson::Value::object();
    level["key"] = sessionKey(snapshot);
    // `hello`'s schema requires name and object_count for the session record; the
    // sidecar fills them from get_editor_state instead of taking them here.
    level["name"] = level["key"];
    level["object_count"] = 0;
    payload["level"] = std::move(level);

    hello["payload"] = std::move(payload);
    return hello.dump(0);
}

/// A request id must be a string or null (SPEC.md 7.3). Anything else is
/// reported as null rather than echoing an invalid id back.
///
/// Note `matjson::Value()` default-constructs an *object*, not null, so the
/// null case must be built explicitly with `nullptr` -- otherwise the reply
/// carries `"id": {}`, which the sidecar rejects as INVALID_REQUEST.
matjson::Value normaliseRequestId(matjson::Value const& id) {
    if (id.isString()) {
        return id;
    }
    log::warn("request carried a non-string id; replying with null");
    return matjson::Value(nullptr);
}

arc::Future<> sendResponse(
    net::WebSocket& socket, matjson::Value const& requestId, matjson::Value payload
) {
    auto frame = matjson::Value::object();
    frame["type"] = "response";
    frame["id"] = requestId;
    frame["payload"] = std::move(payload);
    // A Future<void> takes a bare `co_return`; `co_return co_await x` would try
    // to hand a value to a void promise.
    (void) co_await socket.sendText(frame.dump(0));
    co_return;
}

arc::Future<> sendResult(
    net::WebSocket& socket, matjson::Value const& requestId, matjson::Value result
) {
    auto payload = matjson::Value::object();
    payload["ok"] = true;
    payload["result"] = std::move(result);
    co_await sendResponse(socket, requestId, std::move(payload));
    co_return;
}

arc::Future<> sendError(
    net::WebSocket& socket,
    matjson::Value const& requestId,
    std::string_view code,
    std::string_view message
) {
    auto error = matjson::Value::object();
    error["code"] = std::string(code);
    error["message"] = std::string(message);

    auto payload = matjson::Value::object();
    payload["ok"] = false;
    payload["error"] = std::move(error);
    co_await sendResponse(socket, requestId, std::move(payload));
    co_return;
}

/// Sends a job's failure payload, which is already an `error` object carrying a
/// code and message (see `JobOutcome::failure`).
arc::Future<> sendResponseError(
    net::WebSocket& socket, matjson::Value const& requestId, matjson::Value const& error
) {
    auto payload = matjson::Value::object();
    payload["ok"] = false;
    payload["error"] = error;
    co_await sendResponse(socket, requestId, std::move(payload));
    co_return;
}

/// Splits a `CODE: message` string into its parts.
///
/// The transaction manager and the property layer both report protocol codes
/// inline, so the distinction between, say, `TRANSACTION_ACTIVE` and
/// `TRANSACTION_NOT_REVERTIBLE` survives to the wire.
std::pair<std::string, std::string> splitCode(std::string const& error) {
    auto const colon = error.find(':');
    if (colon == std::string::npos) {
        return { "INVALID_REQUEST", error };
    }
    auto message = error.substr(colon + 1);
    if (!message.empty() && message.front() == ' ') {
        message.erase(0, 1);
    }
    return { error.substr(0, colon), std::move(message) };
}

/// Reads a string field, tolerating its absence and its wrong type alike.
///
/// Every field read out of a sidecar frame goes through this. The sidecar is a
/// separate process that may be a different version, so a missing or mistyped
/// field is a normal condition to survive, not a bug to assert on.
std::string stringField(matjson::Value const& object, char const* key) {
    if (!object.isObject() || !object.contains(key)) {
        return {};
    }
    return object[key].asString().unwrapOr(std::string());
}

} // namespace

Bridge& Bridge::get() {
    static Bridge instance;
    return instance;
}

void Bridge::start() {
    // exchange() makes a concurrent second call a no-op rather than a second
    // connection attempt.
    if (m_active.exchange(true)) {
        return;
    }
    m_stopRequested = false;
    m_gameExiting = false;
    log::info("bridge starting");

    // Dropping the returned handle detaches, so the loop outlives this call.
    async::spawn([this] { return run(configuredUrl()); });
}

void Bridge::stop() {
    if (!m_stopRequested.exchange(true)) {
        log::info("bridge stop requested");
    }
}

void Bridge::onGameExiting() {
    m_gameExiting = true;
    stop();
}

void Bridge::setConnectionStatus(ui::Status status, std::string systemText) {
    auto& host = ui::PanelHost::get();
    host.postStatus(status);

    if (m_lastStatus.has_value() && *m_lastStatus == status) {
        return;
    }
    m_lastStatus = status;

    if (!systemText.empty()) {
        host.postSystem(std::move(systemText));
    }
}

void Bridge::enqueueFrame(std::string frame) {
    {
        std::scoped_lock lock(m_outboundMutex);
        m_outbound.push_back(std::move(frame));
    }
    // Nothing wakes the session loop, so a queued frame waits for the current
    // receive to time out. That is why the poll interval is short (see
    // kPollInterval): it is also the worst-case send latency.
}

void Bridge::sendUserMessage(std::string text) {
    // Rendered immediately rather than echoed back by the sidecar: the user's
    // own message is not level content, so waiting for a round trip would add
    // latency for nothing and read as the UI being slow.
    ui::PanelHost::get().postUserMessage(text);

    auto payload = matjson::Value::object();
    payload["message_id"] = fmt::format("u-{}", m_nextId.fetch_add(1));
    payload["text"] = std::move(text);

    auto frame = matjson::Value::object();
    frame["type"] = "user_message";
    frame["id"] = matjson::Value(nullptr);
    frame["payload"] = std::move(payload);

    log::info("sending user_message");
    enqueueFrame(frame.dump(0));
}

void Bridge::sendCancelTurn() {
    std::string turnId;
    {
        std::scoped_lock lock(m_turnMutex);
        turnId = m_activeTurnId;
    }
    if (turnId.empty()) {
        log::warn("cancel requested with no active turn");
        return;
    }

    auto payload = matjson::Value::object();
    payload["turn_id"] = std::move(turnId);

    auto frame = matjson::Value::object();
    frame["type"] = "cancel_turn";
    frame["id"] = matjson::Value(nullptr);
    frame["payload"] = std::move(payload);

    log::info("sending cancel_turn");
    enqueueFrame(frame.dump(0));
}

void Bridge::sendLevelChanged(int delta) {
    auto payload = matjson::Value::object();
    // The schema wants counts by kind (SPEC.md 7.6). A negative delta means objects
    // went away; a positive one means they appeared. Modifications are not
    // distinguishable by a count, so they are reported as zero rather than guessed.
    payload["added"] = delta > 0 ? delta : 0;
    payload["removed"] = delta < 0 ? -delta : 0;
    payload["modified"] = 0;
    payload["source"] = "user";
    payload["revision"] = editor::LevelRevision::get().current();

    auto frame = matjson::Value::object();
    frame["type"] = "level_changed";
    frame["id"] = matjson::Value(nullptr);
    frame["payload"] = std::move(payload);

    log::info("level changed outside the assistant ({} objects)", delta);
    enqueueFrame(frame.dump(0));
}

void Bridge::sendTransactionReverted(std::string const& txnId) {
    auto payload = matjson::Value::object();
    payload["txn_id"] = txnId;

    auto frame = matjson::Value::object();
    frame["type"] = "transaction_reverted";
    frame["id"] = matjson::Value(nullptr);
    frame["payload"] = std::move(payload);

    // Without this the model keeps describing work that has been undone: it has no
    // other way to learn that its edits are gone (SPEC.md 7.4).
    log::info("transaction {} reverted by the user", txnId);
    enqueueFrame(frame.dump(0));
}

/// Drains queued outbound frames into the socket.
///
/// Called by the watchdog, which is the only thing that can observe the queue
/// while the receive loop is blocked in an await.
arc::Future<> Bridge::flushOutbound(net::WebSocket& socket) {
    std::deque<std::string> pending;
    {
        std::scoped_lock lock(m_outboundMutex);
        pending.swap(m_outbound);
    }

    for (auto& frame : pending) {
        auto sent = co_await socket.sendText(frame);
        if (!sent) {
            log::error("sending a queued frame failed: {}", sent.unwrapErr());
            co_return;
        }
    }
    co_return;
}

arc::Future<> Bridge::watchSession(
    std::shared_ptr<net::WebSocket> socket, std::shared_ptr<SessionState> state
) {
    int ticks = 0;
    while (state->running) {
        co_await arc::sleep(kWachdogInterval);
        if (!state->running) {
            break;
        }

        co_await flushOutbound(*socket);

        if (++ticks % kLivenessPingTicks == 0) {
            if (auto pinged = co_await socket->sendPing(); !pinged) {
                log::info("watchdog: the sidecar is gone ({}); ending the session",
                          pinged.unwrapErr());
                break;
            }
        }

        if (m_stopRequested) {
            log::info("watchdog: stop requested; ending the session");
            break;
        }

        if (m_gameExiting) {
            // The game is going down, so do not hop to the main thread: nothing
            // may be left to service the callback, which would hang instead.
            log::info("watchdog: the game is exiting; ending the session");
            break;
        }

        // `LevelEditorLayer` is destroyed when the user leaves the editor and
        // does not declare a bindable `onExit` (see onGameExiting), so the
        // editor's presence is polled.
        auto editorOpen = co_await async::waitForMainThread<bool>(
            [] { return LevelEditorLayer::get() != nullptr; }
        );
        if (!editorOpen) {
            log::warn("watchdog: could not check the editor; ending the session");
            break;
        }
        if (!*editorOpen) {
            log::info("watchdog: the editor closed; ending the session");
            m_stopRequested = true;
            break;
        }
    }

    // Teardown, exactly once. Whichever of the two tasks gets here first does
    // it; the other finds the flag set and does nothing.
    if (state->tornDown.exchange(true)) {
        co_return;
    }

    // SPEC.md 7.8 step 6: tell the sidecar before closing. Events carry a null
    // id (7.3); the default-constructed Value is an object, so build it explicitly.
    auto closed = matjson::Value::object();
    closed["type"] = "editor_closed";
    closed["id"] = matjson::Value(nullptr);
    closed["payload"] = matjson::Value::object();

    // Best effort: if the socket is already gone there is nobody to tell, and
    // that is not worth an error line.
    if (auto sent = co_await socket->sendText(closed.dump(0)); !sent) {
        log::debug("editor_closed not delivered: {}", sent.unwrapErr());
    }
    (void) co_await socket->close();

    // Unblocks the receive loop, which is the only way it can be woken.
    socket->forceClose();
    co_return;
}

arc::Future<> Bridge::run(std::string url) {
    auto backoffSecs = kInitialBackoffSecs;

    while (!m_stopRequested) {
        auto const path = tokenFilePath();
        // Off-runtime: a slow disk must never stall the async runtime.
        auto token = co_await async::runtime().spawnBlocking<std::string>(
            [path] { return readTokenFile(path); }
        );

        if (token.empty()) {
            log::warn(
                "no handshake token at {} -- start the sidecar first; retrying in {}s",
                path.string(), backoffSecs
            );
            setConnectionStatus(
                ui::Status::Disconnected,
                "Can't reach the sidecar. Start it, then wait a moment while this retries."
            );
        } else {
            auto connected = co_await net::WebSocket::connect(url);
            if (!connected) {
                log::info(
                    "sidecar unreachable at {} ({}); retrying in {}s",
                    url, connected.unwrapErr(), backoffSecs
                );
                setConnectionStatus(
                    ui::Status::Disconnected,
                    fmt::format("Can't reach the sidecar at {}. Retrying...", url)
                );
            } else {
                log::info("connected to the sidecar at {}", url);
                // Reset only after a successful connect, so a sidecar that
                // accepts and immediately drops us still backs off.
                backoffSecs = kInitialBackoffSecs;
                co_await session(
                    std::make_shared<net::WebSocket>(std::move(connected).unwrap()), token
                );

                if (m_stopRequested) {
                    break;
                }
                log::info("sidecar connection lost; reconnecting in {}s", backoffSecs);
                setConnectionStatus(
                    ui::Status::Reconnecting,
                    fmt::format("Connection lost. Reconnecting in {}s...", backoffSecs)
                );
            }
        }

        // In short slices, so a stop (the game exiting, the editor closing) is seen
        // within a quarter second instead of after the rest of a backoff of up to 30 s.
        for (int slice = 0; slice < backoffSecs * 4 && !m_stopRequested; ++slice) {
            co_await arc::sleep(asp::time::Duration::fromMillis(250));
        }
        backoffSecs = std::min(backoffSecs * 2, kMaxBackoffSecs);
    }

    m_active = false;
    log::info("bridge stopped");
}

arc::Future<> Bridge::session(std::shared_ptr<net::WebSocket> socket, std::string token) {
    // Read the level identity on the main thread, for the hello payload.
    auto snapshot = co_await async::waitForMainThread<editor::Snapshot>(
        [] {
            // Developer probes are opt-in and destructive, so the gate is an exact
            // string match rather than a boolean.
            //
            // With a `bool` setting whose declared default was `false` and with no
            // saved value on disk, the probes nonetheless ran on every editor open
            // -- wiping the undo stack and adding objects to the user's level. A
            // string that must equal `run-probes` cannot fail that way, and makes
            // the intent legible in the settings UI as well.
            auto const token = Mod::get()->getSettingValue<std::string>("probe-token");
            // Both spellings: Geode's settings text field does not accept a hyphen, so
            // the value a user can actually enter is `colourprobe`. Accepting the
            // hyphenated form too costs nothing and keeps the docs consistent.
            if (token == "colourprobe" || token == "colour-probe") {
                // Read-only, so unlike the other probe this one is safe to run on a
                // level the user cares about. Separate token rather than a flag on the
                // existing one, precisely so it cannot drag the destructive probes in
                // with it.
                editor::runColorProbe();
            } else if (token == "run-probes") {
                log::warn(
                    "developer probes enabled by probe-token: this will modify the "
                    "level and clear the undo stack"
                );
                runDeveloperProbesOnce();
            }
            return editor::takeSnapshot();
        }
    );
    if (!snapshot) {
        log::error("could not read editor state on the main thread; closing the session");
        (void) co_await socket->close();
        co_return;
    }

    if (auto sent = co_await socket->sendText(buildHello(token, *snapshot)); !sent) {
        log::error("sending hello failed: {}", sent.unwrapErr());
        co_return;
    }
    log::info("sent hello for session '{}'", sessionKey(*snapshot));

    // The watchdog owns everything that has to happen *while* we are blocked in
    // the receive below: flushing queued frames, and noticing that the session
    // should end. Spawned rather than awaited, so the two run concurrently; the
    // shared_ptrs are what make that safe, since the watchdog outlives nothing but
    // keeps both the socket and its own session state alive on its own.
    auto state = std::make_shared<SessionState>();
    async::spawn([this, socket, state] { return watchSession(socket, state); });

    while (!m_stopRequested) {
        // Deliberately no timeout: cancelling this await would corrupt the
        // stream in two separate ways. See `net::WebSocket`.
        auto message = co_await socket->receiveText();
        if (!message) {
            log::info("session ended: {}", message.unwrapErr());
            break;
        }
        co_await handleFrame(message.unwrap(), *socket);
    }

    // Tells the watchdog to finish. It performs the teardown, because it is the
    // task that can still send once this one has stopped reading.
    state->running = false;
}

arc::Future<> Bridge::handleFrame(std::string const& frame, net::WebSocket& socket) {
    auto parsed = matjson::parse(frame);
    if (!parsed) {
        log::warn("dropping unparseable frame: {}", parsed.unwrapErr());
        co_return;
    }
    auto json = parsed.unwrap();
    if (!json.isObject()) {
        log::warn("dropping non-object frame");
        co_return;
    }

    auto const type = json["type"].asString().unwrapOr(std::string());
    if (type.empty()) {
        log::warn("dropping frame with no usable type");
        co_return;
    }

    // Read once, for every kind of frame. Events need the payload as much as
    // requests do.
    auto const payload = json.contains("payload") ? json["payload"] : matjson::Value::object();

    // A response answers a request we issued, so it is resolved rather than
    // answered. Replying to a reply would be wrong: two sides that both answer
    // each other's replies loop without bound. That loop is not hypothetical --
    // an early build of this mod triggered it at roughly 200 frames a second.
    //
    // Checked before normalising the id, so a response frame does not trip the
    // malformed-id warning below.
    if (type == "response") {
        auto const responseId = json["id"].asString().unwrapOr(std::string());
        if (responseId == kHelloRequestId) {
            applyHelloResult(payload);
        } else {
            log::debug("ignoring response to unknown request '{}'", responseId);
        }
        co_return;
    }

    // Events carry a null id and expect no reply (SPEC.md 7.3).
    if (json["id"].isNull()) {
        // Not a coroutine: events are rendered, not answered, so there is
        // nothing here to await.
        handleEvent(type, payload);
        co_return;
    }

    auto const requestId = normaliseRequestId(json["id"]);

    if (type == "ping") {
        auto result = matjson::Value::object();
        result["pong"] = true;
        co_return co_await sendResult(socket, requestId, std::move(result));
    }

    if (type == "get_editor_state") {
        // Editor reads must happen on the main thread (SPEC.md 6.1.3 rule 2).
        auto snapshot = co_await async::waitForMainThread<editor::Snapshot>(
            [] { return editor::takeSnapshot(); }
        );
        if (!snapshot) {
            co_return co_await sendError(
                socket, requestId, "THREAD_DISPATCH_FAILED",
                "could not schedule work on the main thread"
            );
        }
        if (!snapshot->editorOpen) {
            co_return co_await sendError(
                socket, requestId, "EDITOR_NOT_OPEN", "No editor layer is active."
            );
        }
        log::info(
            "get_editor_state -> {} objects, layer {}, {} selected, playtesting={}",
            snapshot->objectCount, snapshot->editorLayer, snapshot->selectedCount,
            snapshot->playtesting
        );
        // The sidecar re-reads the level here, so it is no longer stale.
        auto state = editor::toJson(*snapshot);
        state["revision"] = editor::LevelRevision::get().current();
        co_return co_await sendResult(socket, requestId, std::move(state));
    }

    if (type == "list_object_catalog") {
        // The game's own id -> sprite table, paged. Deliberately independent of the
        // editor being open: it describes the game, not the level, so a handshake
        // during a fresh editor session can still obtain it.
        auto const offset = payload.contains("offset") ? payload["offset"].asInt().unwrapOr(0) : 0;
        auto const limit = payload.contains("limit") ? payload["limit"].asInt().unwrapOr(200) : 200;

        auto page = co_await async::waitForMainThread<editor::CatalogPage>(
            [offset, limit] { return editor::takeCatalogPage(static_cast<int>(offset), static_cast<int>(limit)); }
        );
        if (!page) {
            co_return co_await sendError(
                socket, requestId, "THREAD_DISPATCH_FAILED",
                "could not schedule work on the main thread"
            );
        }
        log::info(
            "list_object_catalog: offset {} -> {} entries of {}",
            offset, page->entries.size(), page->total
        );
        co_return co_await sendResult(socket, requestId, editor::catalogPageToJson(*page));
    }

    if (type == "get_color_channel" || type == "set_color_channel") {
        // Colour lives on the level's colour page, not on an object, so these are
        // their own requests rather than object properties (SPEC.md 7.5.2). A
        // main-thread hop like the transaction requests: they touch `GJEffectManager`
        // and re-tint objects, and a caller awaits each response before sending the
        // next request.
        auto const channel = payload.contains("channel")
            ? payload["channel"].asInt().unwrapOr(0)
            : 0;

        if (type == "get_color_channel") {
            auto found = co_await async::waitForMainThread<editor::ColorChannel>(
                [channel] { return editor::readColorChannel(static_cast<int>(channel)); }
            );
            if (!found) {
                co_return co_await sendError(
                    socket, requestId, "THREAD_DISPATCH_FAILED",
                    "could not schedule work on the main thread"
                );
            }
            co_return co_await sendResult(socket, requestId, editor::colorChannelToJson(*found));
        }

        auto const readByte = [&payload](char const* key) {
            return payload.contains(key) ? payload[key].asInt().unwrapOr(0) : 0;
        };
        auto const red = readByte("red");
        auto const green = readByte("green");
        auto const blue = readByte("blue");

        auto outcome = co_await async::waitForMainThread<geode::Result<editor::ColorChannel>>(
            [channel, red, green, blue] {
                return editor::writeColorChannel(
                    static_cast<int>(channel), static_cast<int>(red), static_cast<int>(green),
                    static_cast<int>(blue)
                );
            }
        );
        if (!outcome) {
            co_return co_await sendError(
                socket, requestId, "THREAD_DISPATCH_FAILED",
                "could not schedule work on the main thread"
            );
        }
        if (!outcome->ok()) {
            auto const [code, message] = splitCode(outcome->unwrapErr());
            co_return co_await sendError(socket, requestId, code, message);
        }
        co_return co_await sendResult(socket, requestId, editor::colorChannelToJson(outcome->unwrap()));
    }

    if (type == "capture_screenshot") {
        // A main-thread hop: rendering into a texture touches the renderer, and the
        // panel has to be hidden for the frame that gets captured.
        editor::CaptureRequest request;
        if (payload.contains("region")) {
            request.region = payload["region"].asString().unwrapOr(std::string("screen"));
        }
        if (request.region != "screen" && request.region != "selection") {
            co_return co_await sendError(
                socket, requestId, "INVALID_REQUEST",
                "'region' must be 'screen' or 'selection'"
            );
        }
        if (payload.contains("radius")) {
            request.radius = static_cast<float>(payload["radius"].asDouble().unwrapOr(120.0));
        }
        // Opt-in, so the interface is hidden unless a caller asks for it: the first live
        // test had the model reporting on the toolbars and buttons instead of the level.
        if (payload.contains("include_ui")) {
            request.includeUi = payload["include_ui"].asBool().unwrapOr(false);
        }

        auto captured = co_await async::waitForMainThread<geode::Result<editor::Capture>>(
            [request] { return editor::captureEditor(request); }
        );
        if (!captured) {
            co_return co_await sendError(
                socket, requestId, "THREAD_DISPATCH_FAILED",
                "could not schedule work on the main thread"
            );
        }
        if (!captured->ok()) {
            auto const [code, message] = splitCode(captured->unwrapErr());
            co_return co_await sendError(socket, requestId, code, message);
        }
        co_return co_await sendResult(socket, requestId, editor::captureToJson(captured->unwrap()));
    }

    if (type == "get_level_settings" || type == "set_level_settings") {
        // Level settings live on one object and the game applies them together, so they are
        // a single request rather than one per field (SPEC.md 7.5.5). A main-thread hop
        // like the colour requests: `levelSettingsUpdated` recreates the background and
        // ground layers, which is renderer state.
        // Parsing happens only for a write. A read has an empty payload by definition, and
        // parsing it first made `get_level_settings` fail with "no settings given" -- the
        // parser's own rule, applied to a request that never had any settings to give.
        if (type == "get_level_settings") {
            auto current = co_await async::waitForMainThread<geode::Result<editor::LevelSettings>>(
                [] { return editor::readLevelSettings(); }
            );
            if (!current) {
                co_return co_await sendError(
                    socket, requestId, "THREAD_DISPATCH_FAILED",
                    "could not schedule work on the main thread"
                );
            }
            if (!current->ok()) {
                auto const [code, message] = splitCode(current->unwrapErr());
                co_return co_await sendError(socket, requestId, code, message);
            }
            auto result = matjson::Value::object();
            result["settings"] = editor::levelSettingsToJson(current->unwrap());
            co_return co_await sendResult(socket, requestId, std::move(result));
        }

        auto parsed = editor::parseLevelSettings(payload);
        if (!parsed) {
            auto const [code, message] = splitCode(parsed.unwrapErr());
            co_return co_await sendError(socket, requestId, code, message);
        }

        auto const wanted = parsed.unwrap();
        auto applied = co_await async::waitForMainThread<geode::Result<editor::LevelSettings>>(
            [wanted] { return editor::writeLevelSettings(wanted); }
        );
        if (!applied) {
            co_return co_await sendError(
                socket, requestId, "THREAD_DISPATCH_FAILED",
                "could not schedule work on the main thread"
            );
        }
        if (!applied->ok()) {
            auto const [code, message] = splitCode(applied->unwrapErr());
            co_return co_await sendError(socket, requestId, code, message);
        }
        auto result = matjson::Value::object();
        result["settings"] = editor::levelSettingsToJson(applied->unwrap());
        // Said plainly: settings are not object edits, so the turn's Revert will not undo
        // them. A user who assumes otherwise would be surprised later.
        result["revertible"] = false;
        co_return co_await sendResult(socket, requestId, std::move(result));
    }

    if (type == "get_camera" || type == "set_camera") {
        // The editor camera, so the model can look somewhere other than whatever happens
        // to be on screen. A main-thread hop: both paths touch the scene graph, and
        // moving the camera is renderer state.
        if (type == "get_camera") {
            auto current = co_await async::waitForMainThread<geode::Result<editor::Camera>>(
                [] { return editor::readCamera(); }
            );
            if (!current) {
                co_return co_await sendError(
                    socket, requestId, "THREAD_DISPATCH_FAILED",
                    "could not schedule work on the main thread"
                );
            }
            if (!current->ok()) {
                auto const [code, message] = splitCode(current->unwrapErr());
                co_return co_await sendError(socket, requestId, code, message);
            }
            auto result = matjson::Value::object();
            result["camera"] = editor::cameraToJson(current->unwrap());
            co_return co_await sendResult(socket, requestId, std::move(result));
        }

        auto const readOptional = [&payload](char const* key) -> std::optional<double> {
            if (!payload.contains(key)) {
                return std::nullopt;
            }
            auto value = payload[key].asDouble();
            if (!value.ok()) {
                return std::nullopt;
            }
            return value.unwrap();
        };

        auto const x = readOptional("x");
        auto const y = readOptional("y");
        auto const zoomValue = readOptional("zoom");
        auto const zoom = zoomValue.has_value()
            ? std::optional<float>(static_cast<float>(*zoomValue))
            : std::nullopt;

        if (!x.has_value() && !y.has_value() && !zoom.has_value()) {
            co_return co_await sendError(
                socket, requestId, "INVALID_REQUEST",
                "give at least one of 'x', 'y' or 'zoom'"
            );
        }

        auto moved = co_await async::waitForMainThread<geode::Result<editor::Camera>>(
            [x, y, zoom] { return editor::writeCamera(x, y, zoom); }
        );
        if (!moved) {
            co_return co_await sendError(
                socket, requestId, "THREAD_DISPATCH_FAILED",
                "could not schedule work on the main thread"
            );
        }
        if (!moved->ok()) {
            auto const [code, message] = splitCode(moved->unwrapErr());
            co_return co_await sendError(socket, requestId, code, message);
        }
        auto result = matjson::Value::object();
        result["camera"] = editor::cameraToJson(moved->unwrap());
        co_return co_await sendResult(socket, requestId, std::move(result));
    }

    if (exec::isImplementedRequest(type)) {
        // The transaction requests (SPEC.md 7.5) are state changes on the
        // manager rather than editor mutations, so they take a main-thread hop
        // instead of a queued job. Ordering is still safe: a caller awaits each
        // response before sending the next request.
        if (type == "begin_transaction") {
            auto const label = payload.contains("label")
                ? payload["label"].asString().unwrapOr(std::string("turn"))
                : std::string("turn");
            auto opened = co_await async::waitForMainThread<geode::Result<std::string, std::string>>(
                [label] { return editor::TransactionManager::get().begin(label); }
            );
            if (!opened) {
                co_return co_await sendError(
                    socket, requestId, "THREAD_DISPATCH_FAILED",
                    "could not schedule work on the main thread"
                );
            }
            if (!opened->ok()) {
                auto const [code, message] = splitCode(opened->unwrapErr());
                co_return co_await sendError(socket, requestId, code, message);
            }
            auto result = matjson::Value::object();
            result["txn_id"] = opened->unwrap();
            co_return co_await sendResult(socket, requestId, std::move(result));
        }

        if (type == "end_transaction" || type == "revert_transaction") {
            if (!payload.contains("txn_id")) {
                co_return co_await sendError(
                    socket, requestId, "INVALID_REQUEST", "'txn_id' is required"
                );
            }
            auto const txnId = payload["txn_id"].asString().unwrapOr(std::string());
            if (txnId.empty()) {
                co_return co_await sendError(
                    socket, requestId, "INVALID_REQUEST", "'txn_id' must be a string"
                );
            }

            auto const isEnd = type == "end_transaction";
            auto outcome = co_await async::waitForMainThread<geode::Result<matjson::Value, std::string>>(
                [txnId, isEnd] {
                    auto& manager = editor::TransactionManager::get();
                    return isEnd ? manager.end(txnId) : manager.revert(txnId);
                }
            );
            if (!outcome) {
                co_return co_await sendError(
                    socket, requestId, "THREAD_DISPATCH_FAILED",
                    "could not schedule work on the main thread"
                );
            }
            if (!outcome->ok()) {
                auto const [code, message] = splitCode(outcome->unwrapErr());
                co_return co_await sendError(socket, requestId, code, message);
            }
            co_return co_await sendResult(socket, requestId, outcome->unwrap());
        }

        // Editor mutations run through the job queue, which slices them across
        // frames so a large request cannot stall one (SPEC.md 6.1.4, A1). The
        // payload was already validated in `makeJob`, off the main thread; the
        // queue then runs it and this coroutine waits for the outcome, so the
        // socket is never touched from the main thread.
        auto job = exec::makeJob(type, payload);
        if (job == nullptr) {
            co_return co_await sendError(
                socket, requestId, "INVALID_REQUEST",
                fmt::format("'{}' could not be prepared", type)
            );
        }
        auto outcome = co_await exec::JobQueue::get().submit(std::move(job));
        if (outcome.ok) {
            co_return co_await sendResult(socket, requestId, std::move(outcome.payload));
        }
        // The job's payload is an error object with a code and message.
        co_return co_await sendResponseError(socket, requestId, outcome.payload);
    }

    if (exec::isDesignRequest(type)) {
        // Read-only, but still queued: both read the editor across many objects, so
        // they are sliced across frames like any other bulk job (SPEC.md 6.1.4).
        auto job = exec::makeDesignJob(type, payload);
        if (job == nullptr) {
            co_return co_await sendError(
                socket, requestId, "INVALID_REQUEST",
                fmt::format("'{}' could not be prepared", type)
            );
        }
        auto outcome = co_await exec::JobQueue::get().submit(std::move(job));
        if (outcome.ok) {
            co_return co_await sendResult(socket, requestId, std::move(outcome.payload));
        }
        co_return co_await sendResponseError(socket, requestId, outcome.payload);
    }

    // A distinct code rather than INVALID_REQUEST: the sidecar may be newer than this
    // mod, and "I do not implement this" lets it hide the dependent tools instead of
    // treating the request as malformed (LEVEL-DESIGN-SPEC.md LD14).
    log::info("unhandled request type '{}'", type);
    co_return co_await sendError(
        socket, requestId, "UNSUPPORTED_REQUEST",
        fmt::format("'{}' is not implemented by this mod version", type)
    );
}

void Bridge::applyHelloResult(matjson::Value const& payload) {
    // A failed handshake is reported as a close code, not a response, so an
    // `ok: false` hello means the sidecar is a version we cannot talk to.
    if (!payload.contains("ok") || !payload["ok"].asBool().unwrapOr(false)) {
        auto const message = payload.contains("error")
            ? stringField(payload["error"], "message")
            : std::string("handshake rejected");
        log::error("hello failed: {}", message);
        ui::PanelHost::get().postStatus(ui::Status::NeedsSetup);
        ui::PanelHost::get().postError(message);
        return;
    }

    auto const result = payload.contains("result")
        ? payload["result"]
        : matjson::Value::object();

    auto const state = stringField(result, "state");
    auto const detail = stringField(result, "detail");

    log::info("hello accepted: state '{}'", state);

    if (state == "needs_setup") {
        // The sidecar is reachable but has no usable LLM configuration, so the
        // panel is not a connection problem to retry -- it is a setup problem
        // for the user to fix (SPEC.md 8.8).
        ui::PanelHost::get().postStatus(ui::Status::NeedsSetup);
        ui::PanelHost::get().postError(
            detail.empty() ? "The sidecar needs configuration." : detail
        );
    } else if (state == "provider_error") {
        ui::PanelHost::get().postStatus(ui::Status::NeedsSetup);
        ui::PanelHost::get().postError(
            detail.empty() ? "The LLM provider rejected the request." : detail
        );
    } else {
        ui::PanelHost::get().postStatus(ui::Status::Ready);
        ui::PanelHost::get().postSystem("Connected.");
    }
}

void Bridge::handleEvent(std::string const& type, matjson::Value const& payload) {
    auto& host = ui::PanelHost::get();

    if (type == "turn_started") {
        auto turnId = stringField(payload, "turn_id");
        {
            std::scoped_lock lock(m_turnMutex);
            m_activeTurnId = turnId;
        }
        host.postTurnStarted();
        return;
    }

    if (type == "tool_call") {
        // Progress, not content. The panel puts this in the thinking indicator, which is
        // the only thing on screen during a long turn -- so a name that changes is what
        // distinguishes "working" from "hung".
        auto const tool = stringField(payload, "tool");
        if (!tool.empty()) {
            ui::PanelHost::get().postProgress(tool);
        }
        return;
    }

    if (type == "assistant_message") {
        host.postAssistantMessage(stringField(payload, "message_id"), stringField(payload, "text"));
        return;
    }

    if (type == "assistant_message_delta") {
        host.postAssistantDelta(
            stringField(payload, "message_id"), stringField(payload, "delta")
        );
        return;
    }

    if (type == "turn_finished") {
        auto const status = stringField(payload, "status");

        // One line per action (SPEC.md 8.9). The `summary` is sidecar-authored
        // text, so it goes through the panel's sanitiser like any other.
        std::vector<std::string> lines;
        if (payload.contains("actions") && payload["actions"].isArray()) {
            for (auto const& action : payload["actions"]) {
                auto summary = stringField(action, "summary");
                if (summary.empty()) {
                    continue;
                }
                auto count = action.contains("count")
                    ? action["count"].asInt().unwrapOr(0)
                    : 0;
                lines.push_back(count > 1 ? fmt::format("{} (x{})", summary, count) : summary);
            }
        }

        {
            std::scoped_lock lock(m_turnMutex);
            m_activeTurnId.clear();
        }
        // The transaction is what the summary's Revert action undoes, so it has to
        // reach the panel or the action cannot be offered (SPEC.md 8.4).
        auto const txnId = stringField(payload, "txn_id");
        log::info(
            "turn '{}' finished: {} (txn '{}')",
            stringField(payload, "turn_id"),
            status,
            txnId
        );
        host.postTurnFinished(status, std::move(lines), txnId);
        return;
    }

    if (type == "sidecar_status") {
        // Sent when readiness changes mid-session, e.g. the provider starts
        // failing after a successful handshake.
        auto const state = stringField(payload, "state");
        auto const detail = stringField(payload, "detail");
        if (state == "ready") {
            host.postStatus(ui::Status::Ready);
        } else {
            host.postStatus(ui::Status::NeedsSetup);
            host.postError(detail.empty() ? fmt::format("Sidecar state: {}", state) : detail);
        }
        return;
    }

    if (type == "history") {
        // SPEC.md 8.10: the sidecar owns the transcript and replays the tail of
        // it after `hello`. The panel's own list is only a window onto it, so
        // the replay replaces rather than appends -- appending would duplicate
        // anything already rendered this session.
        host.postClear();
        if (payload.contains("messages") && payload["messages"].isArray()) {
            for (auto const& message : payload["messages"]) {
                auto const role = stringField(message, "role");
                auto const text = stringField(message, "text");
                if (role == "user") {
                    host.postUserMessage(text);
                } else {
                    host.postAssistantMessage({}, text);
                }
            }
        }
        return;
    }

    if (type == "system_message") {
        auto const level = stringField(payload, "level");
        auto const text = stringField(payload, "text");
        if (level == "error") {
            host.postError(text);
        } else {
            host.postSystem(text);
        }
        return;
    }

    // Unknown events are logged rather than answered: an event has no reply to
    // send, and a newer sidecar may legitimately send types this build predates.
    log::debug("ignoring event '{}'", type);
}

} // namespace gdai::bridge
