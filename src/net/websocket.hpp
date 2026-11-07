#pragma once

#include <Geode/Result.hpp>
#include <arc/future/Future.hpp>
#include <arc/net/TcpStream.hpp>

#include <cstdint>
#include <atomic>
#include <memory>
#include <mutex>
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
///
/// **A receive must never be cancelled.** This is not a style preference; it is
/// the one rule that keeps the stream parseable, and violating it cost a day.
/// `receiveText` is a multi-await operation that keeps its progress in locals
/// (`readMessage` reads a 2-byte header, then the extended length, then the
/// payload) and that answers pings by *writing* to the socket. Destroying it
/// midway therefore does two things: it discards the bytes already consumed from
/// `m_buffer`, so the next call reads a payload byte as a frame header, and if
/// the cancellation lands during the pong write it leaves a *partial frame* on
/// the wire, which desynchronises the peer permanently. The symptom was the
/// sidecar closing with "keepalive ping timeout" two minutes into every session,
/// which looks like a network fault and is not one.
///
/// Use `forceClose` to end a session instead of cancelling a receive: shutting
/// the stream down makes the pending read fail, which is a clean exit.
class WebSocket {
public:
    /// Connects and performs the opening handshake. `url` is `ws://host:port[/path]`.
    static arc::Future<Result<WebSocket>> connect(std::string_view url);

    /// Sends one complete text message, as a single masked frame.
    arc::Future<Result<>> sendText(std::string_view text);

    /// Receives one complete text message, transparently answering pings and
    /// reassembling fragmented frames. See the class comment: never cancel this.
    arc::Future<Result<std::string>> receiveText();

    /// Sends a ping frame; the peer's pong is consumed by `receiveText`.
    ///
    /// Exists for liveness, not latency: on Windows a pending receive is not
    /// always woken when the peer's process dies (seen with a force-killed
    /// sidecar: the socket was gone from netstat, the receive never returned).
    /// A *send* on such a socket fails at once, so the watchdog pings
    /// periodically and ends the session when that fails.
    arc::Future<Result<>> sendPing();

    /// Sends a close frame. Does not wait for the peer's close, so it is
    /// safe to call from a cleanup path.
    arc::Future<Result<>> close(std::uint16_t code = 1000, std::string_view reason = {});

    /// Shuts the underlying stream down, causing any pending receive to fail.
    ///
    /// This is how a session is ended from outside its receive loop. Idempotent,
    /// and safe to call from a thread other than the one awaiting the receive,
    /// which is the point: the callers that need it (a watchdog noticing the
    /// editor closed, the game exiting) are not the receive loop.
    void forceClose();

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

    /// Cross-thread state, behind a pointer so that `WebSocket` stays movable.
    ///
    /// A `std::mutex` member would implicitly delete the move constructor, and
    /// `connect` has to return the socket by value inside a `Result`. Indirection
    /// is the cheap way out; nothing here is hot enough for an extra dereference
    /// to matter.
    struct Shared {
        /// Serialises writes. Two coroutines can be sending at once -- the
        /// session loop answering a request, and the watchdog sending
        /// `editor_closed` -- and interleaving two `sendAll` calls would put a
        /// partial frame on the wire, which is the failure mode this whole file
        /// now exists to avoid.
        ///
        /// Held across the send's own await. That is deliberate: the lock's
        /// purpose is to keep whole frames contiguous. Contention is rare and
        /// each send is small, so the wait is short, but it does mean a send must
        /// never grow into a long operation.
        std::mutex sendMutex;

        /// Set by `forceClose` (and by `close`) so repeated calls are harmless.
        std::atomic<bool> closed { false };
    };

    std::shared_ptr<Shared> m_shared { std::make_shared<Shared>() };
};

} // namespace gdai::net
