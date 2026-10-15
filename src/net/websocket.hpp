#pragma once

#include <Geode/Result.hpp>
#include <arc/future/Future.hpp>
#include <arc/net/TcpStream.hpp>

#include <cstdint>
#include <string>
#include <string_view>

namespace gdai::net {

/// Errors are human-readable strings; they reach the panel as message text and
/// are logged with the request correlation ID (SPEC.md 11).
template <typename T = void>
using Result = geode::Result<T, std::string>;

// `Ok`/`Err` live in namespace `geode`; unqualified use from inside `gdai::net`
// would not find them, because ADL for these arguments only searches gdai::net.
using geode::Err;
using geode::Ok;

/// Minimal RFC 6455 WebSocket **client** for the mod/sidecar bridge.
///
/// Scope is deliberately narrow, matching what the bridge actually needs:
///   - one connection, loopback only, plain `ws://` (no TLS)
///   - text frames carrying UTF-8 JSON
///   - handshake, ping/pong, close, and fragmentation reassembly
///   - no extensions, no permessage-deflate, no subprotocols
///
/// Why hand-rolled: Geode's `web::WebRequest` is HTTP-only and the SDK ships no
/// WebSocket client, and a third-party library would bring its own thread and
/// event loop, which SPEC.md 6.1.3 rule 1 forbids touching Cocos2d and would
/// force a second concurrency model alongside Arc. This runs natively on Arc
/// (`arc::TcpStream`), so there is nothing to bridge.
///
/// Threading: every method is a coroutine and must be awaited on the async
/// runtime. Nothing here touches Cocos2d or GD objects (SPEC.md 6.1.3).
class WebSocket {
public:
    /// Connects and performs the opening handshake. `url` is `ws://host:port[/path]`.
    static arc::Future<Result<WebSocket>> connect(std::string_view url);

    /// Sends one complete text message, as a single masked frame.
    arc::Future<Result<>> sendText(std::string_view text);

    /// Receives one complete text message, transparently answering pings and
    /// reassembling fragmented frames.
    arc::Future<Result<std::string>> receiveText();

    /// Sends a close frame. Does not wait for the peer's close, so it is
    /// safe to call from a cleanup path.
    arc::Future<Result<>> close(std::uint16_t code = 1000, std::string_view reason = {});

    WebSocket(WebSocket&&) noexcept = default;
    WebSocket& operator=(WebSocket&&) noexcept = default;
    WebSocket(WebSocket const&) = delete;
    WebSocket& operator=(WebSocket const&) = delete;

private:
    explicit WebSocket(arc::TcpStream stream) : m_stream(std::move(stream)) {}

    arc::Future<Result<>> performHandshake(
        std::string_view host, std::uint16_t port, std::string_view path
    );
    arc::Future<Result<>> sendFrame(std::uint8_t opcode, std::string_view payload);
    arc::Future<Result<std::string>> readMessage();

    /// Ensures at least `count` bytes are buffered, reading from the socket as
    /// needed. Errors if the peer closes first.
    arc::Future<Result<>> ensureBuffered(std::size_t count);
    arc::Future<Result<std::string>> readExactly(std::size_t count);
    arc::Future<Result<std::string>> readUntil(std::string_view delimiter);

    arc::TcpStream m_stream;
    std::string m_buffer;
};

} // namespace gdai::net
