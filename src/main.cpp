#include <Geode/Geode.hpp>
#include <Geode/utils/async.hpp>

#include <matjson.hpp>

#include <cctype>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>

#include "net/sha1.hpp"
#include "net/websocket.hpp"

using namespace geode::prelude;

namespace {

/// The sidecar is the WebSocket server; the mod is the client (D1).
constexpr std::string_view kSidecarUrl = "ws://127.0.0.1:8765";

/// `%LOCALAPPDATA%\gd-ai-assistant\token` (SPEC.md 7.2). The token is a
/// handshake secret, not the LLM API key.
std::filesystem::path tokenFilePath() {
    // Geode's helper rather than std::getenv, which MSVC flags as unsafe.
    auto const localAppData = geode::utils::getEnvironmentVariable("LOCALAPPDATA");
    if (!localAppData.empty()) {
        return std::filesystem::path(localAppData) / "gd-ai-assistant" / "token";
    }
    return std::filesystem::path("gd-ai-assistant") / "token";
}

/// Builds the `hello` frame (SPEC.md 7.2).
std::string buildHello(std::string_view token) {
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

    // The spike runs outside the editor, so the session key is a placeholder.
    auto level = matjson::Value::object();
    level["key"] = "spike";
    level["name"] = "spike";
    level["object_count"] = 0;
    payload["level"] = std::move(level);

    hello["payload"] = std::move(payload);
    return hello.dump(0);
}

/// Phase-0 spike S2: prove the mod can complete a WebSocket handshake against
/// the Python sidecar and exchange JSON frames.
///
/// This is a transport proof, not the bridge: it runs once on mod load, logs
/// each stage, and is replaced by the real connection manager in Phase 1.
arc::Future<> runHandshakeSpike() {
    auto const tokenPath = tokenFilePath();

    // Read the token on the blocking pool, so a slow disk never stalls the
    // async runtime (SPEC.md 6.1.3 rule 1).
    auto token = co_await async::runtime().spawnBlocking<std::string>(
        [tokenPath]() -> std::string {
            std::ifstream file(tokenPath, std::ios::binary);
            // Bind the iterators to named variables first: writing
            // `std::string(first, last)` directly would parse as a function
            // declaration (most vexing parse), not a variable.
            auto const first = std::istreambuf_iterator<char>(file);
            auto const last = std::istreambuf_iterator<char>();
            std::string contents(first, last);
            while (!contents.empty() && std::isspace(static_cast<unsigned char>(contents.back()))) {
                contents.pop_back();
            }
            return contents;
        }
    );

    if (token.empty()) {
        log::error(
            "S2 spike: no handshake token at {} -- start the sidecar first",
            tokenPath.string()
        );
        co_return;
    }
    log::info("S2 spike: read handshake token from {}", tokenPath.string());

    auto connected = co_await gdai::net::WebSocket::connect(kSidecarUrl);
    if (!connected) {
        log::error("S2 spike: connect failed: {}", connected.unwrapErr());
        co_return;
    }
    auto socket = std::move(connected).unwrap();
    log::info("S2 spike: handshake accepted (Sec-WebSocket-Accept verified)");

    if (auto sent = co_await socket.sendText(buildHello(token)); !sent) {
        log::error("S2 spike: sending hello failed: {}", sent.unwrapErr());
        co_return;
    }
    log::info("S2 spike: sent hello");

    // The sidecar replies with the hello response, then pushes history.
    for (auto round = 0; round < 2; ++round) {
        auto message = co_await socket.receiveText();
        if (!message) {
            log::error("S2 spike: receive failed: {}", message.unwrapErr());
            co_return;
        }
        log::info("S2 spike: received {}", message.unwrap());
    }

    if (auto closed = co_await socket.close(); !closed) {
        log::warn("S2 spike: close failed: {}", closed.unwrapErr());
    }
    log::info("S2 spike: round trip complete -- S2 transport proof succeeded");
}

} // namespace

$on_mod(Loaded) {
    log::info("GD AI Assistant {} loaded", Mod::get()->getVersion().toVString());

    // The handshake's integrity depends on SHA-1, which neither Geode nor Arc
    // provides. Refuse to connect on a bad digest rather than handshaking with
    // a wrong one.
    if (!gdai::net::sha1SelfTest()) {
        log::error("SHA-1 self-test FAILED; refusing to attempt the WebSocket handshake");
        return;
    }
    log::info("SHA-1 self-test passed");

    // Dropping the TaskHandle detaches rather than cancels, so the spike runs
    // to completion without being kept alive here.
    async::spawn(runHandshakeSpike);
}
