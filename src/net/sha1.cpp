#include "sha1.hpp"

#include <fmt/format.h>

#include <cstddef>
#include <iterator>
#include <vector>

namespace gdai::net {
namespace {

/// Per-round constants from FIPS 180-1: one per 20-round block.
constexpr std::array<std::uint32_t, 4> kRoundConstants {
    0x5A827999u, 0x6ED9EBA1u, 0x8F1BBCDCu, 0xCA62C1D6u,
};

constexpr std::uint32_t rotateLeft(std::uint32_t value, unsigned bits) {
    return (value << bits) | (value >> (32u - bits));
}

} // namespace

Sha1Digest sha1(std::string_view data) {
    // Initial hash values (FIPS 180-1).
    std::uint32_t h0 = 0x67452301u;
    std::uint32_t h1 = 0xEFCDAB89u;
    std::uint32_t h2 = 0x98BADCFEu;
    std::uint32_t h3 = 0x10325476u;
    std::uint32_t h4 = 0xC3D2E1F0u;

    // Pad the message: data || 0x80 || zeros || 64-bit big-endian bit length,
    // so the total length is a multiple of 64 bytes.
    std::vector<std::uint8_t> message(data.begin(), data.end());
    auto const bitLength = static_cast<std::uint64_t>(data.size()) * 8u;
    message.push_back(0x80u);
    while (message.size() % 64u != 56u) {
        message.push_back(0x00u);
    }
    for (int shift = 56; shift >= 0; shift -= 8) {
        message.push_back(static_cast<std::uint8_t>((bitLength >> shift) & 0xFFu));
    }

    // Process each 512-bit block.
    for (std::size_t offset = 0; offset < message.size(); offset += 64) {
        std::array<std::uint32_t, 80> w {};
        for (std::size_t i = 0; i < 16; ++i) {
            auto const base = offset + i * 4;
            w[i] = (static_cast<std::uint32_t>(message[base]) << 24)
                 | (static_cast<std::uint32_t>(message[base + 1]) << 16)
                 | (static_cast<std::uint32_t>(message[base + 2]) << 8)
                 | static_cast<std::uint32_t>(message[base + 3]);
        }
        for (std::size_t i = 16; i < 80; ++i) {
            w[i] = rotateLeft(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
        }

        auto a = h0;
        auto b = h1;
        auto c = h2;
        auto d = h3;
        auto e = h4;

        for (std::size_t i = 0; i < 80; ++i) {
            std::uint32_t f = 0;
            std::uint32_t k = 0;
            if (i < 20) {
                f = (b & c) | (~b & d);
                k = kRoundConstants[0];
            } else if (i < 40) {
                f = b ^ c ^ d;
                k = kRoundConstants[1];
            } else if (i < 60) {
                f = (b & c) | (b & d) | (c & d);
                k = kRoundConstants[2];
            } else {
                f = b ^ c ^ d;
                k = kRoundConstants[3];
            }
            auto const temp = rotateLeft(a, 5) + f + e + k + w[i];
            e = d;
            d = c;
            c = rotateLeft(b, 30);
            b = a;
            a = temp;
        }

        h0 += a;
        h1 += b;
        h2 += c;
        h3 += d;
        h4 += e;
    }

    std::array<std::uint32_t, 5> const words { h0, h1, h2, h3, h4 };
    Sha1Digest digest {};
    for (std::size_t i = 0; i < words.size(); ++i) {
        digest[i * 4] = static_cast<std::uint8_t>((words[i] >> 24) & 0xFFu);
        digest[i * 4 + 1] = static_cast<std::uint8_t>((words[i] >> 16) & 0xFFu);
        digest[i * 4 + 2] = static_cast<std::uint8_t>((words[i] >> 8) & 0xFFu);
        digest[i * 4 + 3] = static_cast<std::uint8_t>(words[i] & 0xFFu);
    }
    return digest;
}

std::string toHex(Sha1Digest const& digest) {
    std::string hex;
    hex.reserve(digest.size() * 2);
    for (auto byte : digest) {
        fmt::format_to(std::back_inserter(hex), "{:02x}", byte);
    }
    return hex;
}

bool sha1SelfTest() {
    // FIPS 180-1 / RFC 3174 published vectors.
    struct Vector {
        std::string_view input;
        std::string_view expected;
    };
    static constexpr std::array<Vector, 3> kKnownVectors {{
        { "", "da39a3ee5e6b4b0d3255bfef95601890afd80709" },
        { "abc", "a9993e364706816aba3e25717850c26c9cd0d89d" },
        {
            "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
            "84983e441c3bd26ebaae4aa1f95129e5e54670f1",
        },
    }};

    for (auto const& vector : kKnownVectors) {
        if (toHex(sha1(vector.input)) != vector.expected) {
            return false;
        }
    }

    // The RFC 6455 1.3 handshake example: this exact key must produce this
    // exact accept token, which is what the sidecar will verify.
    auto const digest = sha1("dGhlIHNhbXBsZSBub25jZQ==258EAFA5-E914-47DA-95CA-C5AB0DC85B11");
    return toHex(digest) == "b37a4f2cc0624f1690f64606cf385945b2bec4ea";
}

} // namespace gdai::net
