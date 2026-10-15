# Protocol Schemas

Machine-readable JSON Schemas (draft 2020-12) for the mod ⇄ sidecar protocol
specified in `SPEC.md` §7.

## Why this directory exists

A Geode mod cannot run headless in CI, so **the protocol is the test seam**
(`SPEC.md` §13). Both sides validate against these schemas in tests, and the
fake mod / fake sidecar harnesses are driven by them:

| Consumer | Validates |
| --- | --- |
| `sidecar/` | every inbound frame from the mod |
| `src/` (mod) | every inbound frame from the sidecar (debug builds) |
| `tests/fake_mod/` | its own outbound frames, to stay honest |
| `tests/fake_sidecar/` | its own outbound frames |

## Layout

```
v1/
  common.schema.json    envelope, response, error, object record, property schema, enums
  hello.schema.json     handshake (§7.2)
  chat.schema.json      chat channel (§7.4)
  editor.schema.json    editor requests + mod→sidecar events (§7.5, §7.6)
  envelope.schema.json  entry point: envelope discriminated on `type`
```

`envelope.schema.json` is the single validation entry point. Given any decoded
frame, a validator resolves `$defs.registry` by the frame's `type` and checks
the payload.

## Versioning

`protocol_version` is an integer carried in `hello`; the directory name matches
it. A breaking change to any payload shape bumps the version and adds a new
directory — it never edits `v1/` in place, because a shipped mod build may be
talking to a newer sidecar.

## Conventions

- **Encoding:** UTF-8 JSON text frames.
- **Direction:** `direction` on each message definition is `m2s` (mod → sidecar),
  `s2m` (sidecar → mod) or `both`.
- **Requests vs events:** requests carry a non-null `id` and are answered with a
  `response` frame carrying the same `id`. Events carry `id: null`.
- **IDs:** unique per sender; the two senders use disjoint prefixes (`m-`, `s-`)
  so a correlation ID is unambiguous in logs.
- Fields marked `x-unverified` carry an assumption from `SPEC.md` Appendix A
  that a phase-0 spike must still confirm.
