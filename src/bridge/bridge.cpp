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

#include "../editor/editor_state.hpp"

using namespace geode::prelude;

namespace gdai::bridge {
namespace {

/// How long a single receive may block before the loop re-checks the stop flag.
///
/// A bare blocking receive would make `stop()` wait for the sidecar to send
/// something, and an idle sidecar never does. Polling keeps the socket touched
/// by one thread only, which is what makes shutdown safe without a lock.
constexpr auto kPollInterval = asp::time::Duration::fromMillis(250);

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

std::string configuredUrl() {
    auto const url = Mod::get()->getSettingValue<std::string>("sidecar-url");
    return url.empty() ? std::string("ws://127.0.0.1:8765") : url;
}

/// Builds the `hello` frame (SPEC.md 7.2).
std::string buildHello(std::string_view token, editor::Snapshot const& snapshot) {
    auto hello = matjson::Value::object();
    hello["type"] = "hello";
    hello["id"] = "m-1";

    auto payload = matjson::Value::object();
    payload["protocol_version"] = 1;
    payload["token"] = std::string(token);

    auto client = matjson::Value::object();
    client["mod_version"] = Mod::get()->getVersion().toNonVString();
    client["gd_version"] = Loader::get()->getGameVersion();
    client["geode_version"] = Loader::get()->getVersion().toNonVString();
    payload["client"] = std::move(client);

    // Session key defaults to the level name (SPEC.md 6.2.5).
    auto const key = snapshot.levelName.empty() ? std::string("unknown") : snapshot.levelName;
    auto level = matjson::Value::object();
    level["key"] = key;
    level["name"] = key;
    level["object_count"] = snapshot.objectCount;
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
        } else {
            auto connected = co_await net::WebSocket::connect(url);
            if (!connected) {
                log::info(
                    "sidecar unreachable at {} ({}); retrying in {}s",
                    url, connected.unwrapErr(), backoffSecs
                );
            } else {
                log::info("connected to the sidecar at {}", url);
                // Reset only after a successful connect, so a sidecar that
                // accepts and immediately drops us still backs off.
                backoffSecs = kInitialBackoffSecs;
                co_await session(std::move(connected).unwrap(), token);

                if (m_stopRequested) {
                    break;
                }
                log::info("sidecar connection lost; reconnecting in {}s", backoffSecs);
            }
        }

        co_await arc::sleep(asp::time::Duration::fromSecs(backoffSecs));
        backoffSecs = std::min(backoffSecs * 2, kMaxBackoffSecs);
    }

    m_active = false;
    log::info("bridge stopped");
}

arc::Future<> Bridge::session(net::WebSocket socket, std::string token) {
    // Read the level identity on the main thread, for the hello payload.
    auto snapshot = co_await async::waitForMainThread<editor::Snapshot>(
        [] { return editor::takeSnapshot(); }
    );
    if (!snapshot) {
        log::error("could not read editor state on the main thread; closing the session");
        (void) co_await socket.close();
        co_return;
    }

    if (auto sent = co_await socket.sendText(buildHello(token, *snapshot)); !sent) {
        log::error("sending hello failed: {}", sent.unwrapErr());
        co_return;
    }

    while (!m_stopRequested) {
        // Bounded wait, so the stop flag is observed even while idle. A bare
        // blocking receive would never return on an idle sidecar, which is what
        // stopped `stop()` from taking effect.
        auto received = co_await arc::timeout(kPollInterval, socket.receiveText());
        if (!received) {
            if (m_gameExiting) {
                // Do not hop to the main thread while the game is going down:
                // nothing may service the callback, which would hang instead.
                log::info("game is exiting; ending the session");
                break;
            }
            // `LevelEditorLayer` is destroyed when the user leaves the editor.
            // There is no bindable `onExit` for it (see onGameExiting), so the
            // editor's presence is polled instead.
            auto editorOpen = co_await async::waitForMainThread<bool>(
                [] { return LevelEditorLayer::get() != nullptr; }
            );
            if (!editorOpen) {
                log::warn("could not check the editor on the main thread; ending the session");
                break;
            }
            if (!*editorOpen) {
                log::info("editor closed; ending the session");
                stop();
                break;
            }
            continue;
        }

        auto message = std::move(*received);
        if (!message) {
            log::info("session ended: {}", message.unwrapErr());
            break;
        }
        co_await handleFrame(message.unwrap(), socket);
    }

    // SPEC.md 7.8 step 6: tell the sidecar before closing.
    auto closed = matjson::Value::object();
    closed["type"] = "editor_closed";
    // Events carry a null id (7.3); the default-constructed Value is an object.
    closed["id"] = matjson::Value(nullptr);
    closed["payload"] = matjson::Value::object();
    (void) co_await socket.sendText(closed.dump(0));
    (void) co_await socket.close();
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

    // Events carry a null id and need no reply. Responses answer a request we
    // issued -- the mod issues none yet (the job queue arrives in Phase 2), so
    // these are ignored for now. Replying to them would be wrong: it would
    // answer the sidecar's own reply to our hello, and two sides that both
    // answer each other's replies loop without bound.
    //
    // Checked before normalising the id, so event frames do not trip the
    // malformed-id warning below.
    if (type == "response" || json["id"].isNull()) {
        log::debug("ignoring '{}' frame", type);
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
            "get_editor_state -> {} objects, layer {}, {} selected",
            snapshot->objectCount, snapshot->editorLayer, snapshot->selectedCount
        );
        co_return co_await sendResult(socket, requestId, editor::toJson(*snapshot));
    }

    // Phase 2/4 handle the rest; answering with a clear error beats silence,
    // which would strand the caller until its timeout.
    log::info("unhandled request type '{}'", type);
    co_return co_await sendError(
        socket, requestId, "INVALID_REQUEST",
        fmt::format("'{}' is not implemented yet", type)
    );
}

} // namespace gdai::bridge
