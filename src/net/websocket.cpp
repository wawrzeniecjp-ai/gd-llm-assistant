#include "websocket.hpp"

#include "sha1.hpp"

#include <Geode/utils/base64.hpp>
#include <fmt/format.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <random>
#include <span>

namespace gdai::net {
namespace {

/// RFC 6455 1.3: the magic GUID appended to Sec-WebSocket-Key.
constexpr std::string_view kHandshakeGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/// Number of random bytes in a Sec-WebSocket-Key (RFC 6455 4.1).
constexpr std::size_t kKeyBytes = 16;

/// Hard inbound frame cap (SPEC.md 6.1.4). The mod enforces this itself rather
/// than trusting the sidecar, and refuses to buffer past it.
constexpr std::size_t kMaxFramePayload = 4u * 1024u * 1024u;

/// Read chunk size for socket reads.
constexpr std::size_t kReadChunk = 16u * 1024u;

namespace Opcode {
constexpr std::uint8_t Continuation = 0x0;
constexpr std::uint8_t Text = 0x1;
constexpr std::uint8_t Binary = 0x2;
constexpr std::uint8_t Close = 0x8;
constexpr std::uint8_t Ping = 0x9;
constexpr std::uint8_t Pong = 0xA;
} // namespace Opcode

struct ParsedUrl {
    std::string host;
    std::uint16_t port = 80;
    std::string path = "/";
};

/// Parses `ws://host:port[/path]`. Deliberately minimal: no IPv6 literals, no
/// userinfo, no TLS, because the bridge is loopback-only by design (D11).
Result<ParsedUrl> parseUrl(std::string_view url) {
    constexpr std::string_view kScheme = "ws://";
    if (!url.starts_with(kScheme)) {
        return Err(fmt::format("only ws:// URLs are supported, got '{}'", url));
    }

    auto rest = url.substr(kScheme.size());
    auto const slash = rest.find('/');
    auto const authority = slash == std::string_view::npos ? rest : rest.substr(0, slash);

    ParsedUrl parsed;
    if (slash != std::string_view::npos) {
        parsed.path = std::string(rest.substr(slash));
    }

    auto const colon = authority.rfind(':');
    if (colon == std::string_view::npos) {
        parsed.host = std::string(authority);
    } else {
        parsed.host = std::string(authority.substr(0, colon));
        auto const portText = authority.substr(colon + 1);
        unsigned value = 0;
        for (auto const c : portText) {
            if (c < '0' || c > '9') {
                return Err(fmt::format("invalid port in '{}'", url));
            }
            value = value * 10u + static_cast<unsigned>(c - '0');
            if (value > 65535u) {
                return Err(fmt::format("port out of range in '{}'", url));
            }
        }
        if (value == 0) {
            return Err(fmt::format("invalid port in '{}'", url));
        }
        parsed.port = static_cast<std::uint16_t>(value);
    }

    if (parsed.host.empty()) {
        return Err(fmt::format("missing host in '{}'", url));
    }
    return Ok(parsed);
}

std::array<std::uint8_t, kKeyBytes> randomKey() {
    std::random_device device;
    std::mt19937_64 generator(device());
    std::uniform_int_distribution<unsigned> distribution(0, 255);

    std::array<std::uint8_t, kKeyBytes> key {};
    for (auto& byte : key) {
        byte = static_cast<std::uint8_t>(distribution(generator));
    }
    return key;
}

std::string base64Encode(std::span<std::uint8_t const> data) {
    return geode::utils::base64::encode(
        data, geode::utils::base64::Base64Variant::Normal
    );
}

std::string toLower(std::string_view text) {
    std::string lowered(text);
    std::transform(lowered.begin(), lowered.end(), lowered.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return lowered;
}

std::string trim(std::string_view text) {
    auto const isSpace = [](unsigned char c) { return std::isspace(c) != 0; };
    auto begin = std::find_if_not(text.begin(), text.end(), isSpace);
    auto end = std::find_if_not(text.rbegin(), text.rend(), isSpace).base();
    if (begin >= end) {
        return {};
    }
    return std::string(begin, end);
}

/// Case-insensitive lookup of a single header value in an HTTP response head.
std::string findHeader(std::string_view head, std::string_view name) {
    auto const wanted = toLower(name);
    std::size_t position = 0;
    while (position < head.size()) {
        auto const lineEnd = head.find("\r\n", position);
        auto const line = head.substr(
            position, lineEnd == std::string_view::npos ? std::string_view::npos : lineEnd - position
        );
        auto const colon = line.find(':');
        if (colon != std::string_view::npos && toLower(line.substr(0, colon)) == wanted) {
            return trim(line.substr(colon + 1));
        }
        if (lineEnd == std::string_view::npos) {
            break;
        }
        position = lineEnd + 2;
    }
    return {};
}

} // namespace

arc::Future<Result<WebSocket>> WebSocket::connect(std::string_view url) {
    auto parsed = parseUrl(url);
    if (!parsed) {
        co_return Err(parsed.unwrapErr());
    }
    auto const endpoint = parsed.unwrap();

    auto const address = fmt::format("{}:{}", endpoint.host, endpoint.port);
    auto connected = co_await arc::TcpStream::connect(address);
    if (!connected) {
        co_return Err(fmt::format("connect to {} failed: {}", address, connected.unwrapErr()));
    }

    WebSocket socket(std::move(connected).unwrap());
    // We exchange small JSON frames; Nagle would add latency for no gain.
    (void)socket.m_stream.setNoDelay(true);

    if (auto handshake = co_await socket.performHandshake(
            endpoint.host, endpoint.port, endpoint.path
        );
        !handshake) {
        co_return Err(handshake.unwrapErr());
    }

    co_return Ok(std::move(socket));
}

arc::Future<Result<>> WebSocket::performHandshake(
    std::string_view host, std::uint16_t port, std::string_view path
) {
    auto const key = randomKey();
    auto const keyText = base64Encode(std::span<std::uint8_t const>(key.data(), key.size()));

    // Note: no Origin header is sent. That is deliberate and load-bearing --
    // the sidecar rejects any upgrade carrying Origin, which is what stops a
    // local web page from driving the editor (D11, SPEC.md 12).
    auto const request = fmt::format(
        "GET {} HTTP/1.1\r\n"
        "Host: {}:{}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: {}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n",
        path, host, port, keyText
    );

    if (auto sent = co_await m_stream.sendAll(request.data(), request.size()); !sent) {
        co_return Err(fmt::format("sending handshake failed: {}", sent.unwrapErr()));
    }

    auto headResult = co_await readUntil("\r\n\r\n");
    if (!headResult) {
        co_return Err(headResult.unwrapErr());
    }
    auto const head = headResult.unwrap();

    auto const statusEnd = head.find("\r\n");
    auto const statusLine = head.substr(0, statusEnd);
    if (statusLine.find(" 101 ") == std::string::npos) {
        co_return Err(fmt::format("sidecar refused the upgrade: {}", statusLine));
    }

    // Verify Sec-WebSocket-Accept, so a wrong SHA-1 or base64 fails loudly here
    // rather than as a confusing framing error later.
    auto const digest = sha1(fmt::format("{}{}", keyText, kHandshakeGuid));
    auto const expected = base64Encode(
        std::span<std::uint8_t const>(digest.data(), digest.size())
    );
    auto const actual = findHeader(head, "Sec-WebSocket-Accept");
    if (actual != expected) {
        co_return Err(fmt::format(
            "Sec-WebSocket-Accept mismatch: expected '{}', got '{}'", expected, actual
        ));
    }

    co_return Ok();
}

arc::Future<Result<>> WebSocket::sendFrame(std::uint8_t opcode, std::string_view payload) {
    std::string frame;

    // Client-to-server frames must be masked (RFC 6455 5.3).
    frame.push_back(static_cast<char>(0x80u | opcode));
    auto const length = payload.size();
    if (length < 126) {
        frame.push_back(static_cast<char>(0x80u | length));
    } else if (length <= 0xFFFFu) {
        frame.push_back(static_cast<char>(0x80u | 126u));
        frame.push_back(static_cast<char>((length >> 8) & 0xFFu));
        frame.push_back(static_cast<char>(length & 0xFFu));
    } else {
        frame.push_back(static_cast<char>(0x80u | 127u));
        for (int shift = 56; shift >= 0; shift -= 8) {
            frame.push_back(static_cast<char>((length >> shift) & 0xFFu));
        }
    }

    auto const mask = randomKey();
    frame.append(reinterpret_cast<char const*>(mask.data()), 4);
    for (std::size_t i = 0; i < length; ++i) {
        frame.push_back(static_cast<char>(
            static_cast<std::uint8_t>(payload[i]) ^ mask[i % 4]
        ));
    }

    if (auto sent = co_await m_stream.sendAll(frame.data(), frame.size()); !sent) {
        co_return Err(fmt::format("send failed: {}", sent.unwrapErr()));
    }
    co_return Ok();
}

arc::Future<Result<>> WebSocket::sendText(std::string_view text) {
    co_return co_await sendFrame(Opcode::Text, text);
}

arc::Future<Result<>> WebSocket::close(std::uint16_t code, std::string_view reason) {
    std::string payload;
    payload.push_back(static_cast<char>((code >> 8) & 0xFFu));
    payload.push_back(static_cast<char>(code & 0xFFu));
    payload.append(reason);
    co_return co_await sendFrame(Opcode::Close, payload);
}

arc::Future<Result<std::string>> WebSocket::readMessage() {
    std::string message;

    while (true) {
        auto headerResult = co_await readExactly(2);
        if (!headerResult) {
            co_return Err(headerResult.unwrapErr());
        }
        auto const header = headerResult.unwrap();

        auto const first = static_cast<std::uint8_t>(header[0]);
        auto const second = static_cast<std::uint8_t>(header[1]);
        auto const isFinal = (first & 0x80u) != 0;
        auto const opcode = static_cast<std::uint8_t>(first & 0x0Fu);
        auto const isMasked = (second & 0x80u) != 0;
        std::uint64_t length = second & 0x7Fu;

        // A server must never mask (RFC 6455 5.1).
        if (isMasked) {
            co_return Err("protocol error: server frame was masked");
        }

        if (length == 126u) {
            auto extended = co_await readExactly(2);
            if (!extended) {
                co_return Err(extended.unwrapErr());
            }
            auto const bytes = extended.unwrap();
            length = (static_cast<std::uint64_t>(static_cast<std::uint8_t>(bytes[0])) << 8)
                   | static_cast<std::uint64_t>(static_cast<std::uint8_t>(bytes[1]));
        } else if (length == 127u) {
            auto extended = co_await readExactly(8);
            if (!extended) {
                co_return Err(extended.unwrapErr());
            }
            auto const bytes = extended.unwrap();
            length = 0;
            for (std::size_t i = 0; i < 8; ++i) {
                length = (length << 8) | static_cast<std::uint8_t>(bytes[i]);
            }
        }

        if (length > kMaxFramePayload) {
            // Do not buffer it: refuse and drop the connection (SPEC.md 6.1.4).
            co_return Err(fmt::format(
                "frame of {} bytes exceeds the {}-byte limit", length, kMaxFramePayload
            ));
        }

        auto payloadResult = co_await readExactly(static_cast<std::size_t>(length));
        if (!payloadResult) {
            co_return Err(payloadResult.unwrapErr());
        }
        auto const payload = payloadResult.unwrap();

        switch (opcode) {
            case Opcode::Text:
            case Opcode::Binary:
            case Opcode::Continuation:
                message.append(payload);
                if (isFinal) {
                    co_return Ok(std::move(message));
                }
                break;

            case Opcode::Ping:
                // Answer the ping with a pong carrying the same payload.
                if (auto pong = co_await sendFrame(Opcode::Pong, payload); !pong) {
                    co_return Err(pong.unwrapErr());
                }
                break;

            case Opcode::Pong:
                break;

            case Opcode::Close:
                co_return Err("closed by peer");

            default:
                co_return Err(fmt::format("protocol error: reserved opcode 0x{:X}", opcode));
        }
    }
}

arc::Future<Result<std::string>> WebSocket::receiveText() {
    co_return co_await readMessage();
}

arc::Future<Result<>> WebSocket::ensureBuffered(std::size_t count) {
    std::array<char, kReadChunk> chunk {};
    while (m_buffer.size() < count) {
        auto received = co_await m_stream.receive(chunk.data(), chunk.size());
        if (!received) {
            co_return Err(fmt::format("receive failed: {}", received.unwrapErr()));
        }
        auto const bytes = received.unwrap();
        if (bytes == 0) {
            co_return Err("connection closed by peer");
        }
        m_buffer.append(chunk.data(), bytes);
    }
    co_return Ok();
}

arc::Future<Result<std::string>> WebSocket::readExactly(std::size_t count) {
    auto buffered = co_await ensureBuffered(count);
    if (!buffered) {
        co_return Err(buffered.unwrapErr());
    }
    std::string out(m_buffer, 0, count);
    m_buffer.erase(0, count);
    co_return Ok(std::move(out));
}

arc::Future<Result<std::string>> WebSocket::readUntil(std::string_view delimiter) {
    while (true) {
        auto const found = m_buffer.find(delimiter);
        if (found != std::string::npos) {
            auto const end = found + delimiter.size();
            std::string out(m_buffer, 0, end);
            m_buffer.erase(0, end);
            co_return Ok(std::move(out));
        }
        // Guard against a peer that never terminates the head.
        if (m_buffer.size() > kMaxFramePayload) {
            co_return Err("HTTP response head exceeded the size limit");
        }
        if (auto more = co_await ensureBuffered(m_buffer.size() + 1); !more) {
            co_return Err(more.unwrapErr());
        }
    }
}

} // namespace gdai::net
