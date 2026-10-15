#pragma once

#include <array>
#include <cstdint>
#include <string_view>

namespace gdai::net {

/// A SHA-1 digest: 20 bytes, big-endian word order.
using Sha1Digest = std::array<std::uint8_t, 20>;

/// SHA-1 of the given data (FIPS 180-1 / RFC 3174).
///
/// The WebSocket opening handshake requires SHA-1 (`base64(SHA1(key + GUID))`,
/// RFC 6455 4.2.2). Neither Geode nor Arc provides it -- Geode ships
/// `sha256`/`Sha256Hasher` only -- so the mod carries its own. This is the sole
/// cryptographic primitive the bridge needs: message framing uses none.
///
/// SHA-1 is cryptographically broken for collision resistance, which does not
/// matter here: RFC 6455 uses it as a handshake checksum to prove the peer
/// speaks WebSocket, not as a security control. See SPEC.md 2 (S2).
Sha1Digest sha1(std::string_view data);

/// Formats a digest as lowercase hex, for logs and test comparisons.
std::string toHex(Sha1Digest const& digest);

/// Verifies `sha1` against the published test vectors, and the RFC 6455
/// handshake example. Returns false on any mismatch, so the caller can refuse
/// to handshake rather than trusting a broken digest.
bool sha1SelfTest();

} // namespace gdai::net
