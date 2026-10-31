# Sidecar

The local companion process. It is the WebSocket **server**; the Geode mod is
the client (`SPEC.md` §5, decision D1).

It owns everything LLM-related — API key, model, conversation, tool loop and
session persistence — and never touches game memory, Cocos2d, or the level file.
The API key never leaves this process.

## Run

```powershell
cd sidecar
uv sync
uv run gd-ai-assistant-sidecar
```

It listens on `ws://127.0.0.1:8765` by default and writes the handshake token to
`%LOCALAPPDATA%\gd-ai-assistant\token` on first start. The mod reads that file.

With no `api_key`/`model` configured the sidecar still starts, accepts `hello`,
and reports `state: "needs_setup"` — so the panel shows a setup message rather
than failing to connect (`SPEC.md` §9.2).

## Configure

Copy `.env.example` to `.env` and fill it in. `llm_provider`, `api_key` and
`model` are required for a usable session; `model` deliberately has no default,
to force an explicit, current choice.

## Test

```powershell
uv run pytest
```

`tests/test_protocol.py` validates the JSON Schemas in `../protocol/v1/` and the
example frames from `SPEC.md` §7. Since the mod cannot run headless in CI, the
protocol is the test seam (`SPEC.md` §13).

## Status

Working. Implemented: configuration, protocol schema loading and validation,
token file handling, log redaction, the connection lifecycle through the
handshake (single client, `Origin` rejection, token check, version check,
`ping`, `editor_closed`), the provider adapters (Anthropic, OpenAI,
OpenAI-compatible, and a `scripted` fake), the tool loop with lazy transactions,
per-turn caps, timeouts, retry with jitter and cancellation, the object catalog,
and the system prompt.

Not implemented yet: session persistence to SQLite (`session_dir` is currently
unused, so history is in-memory and lost on restart), streaming deltas, and
level-change notifications from the mod — so the assistant is not told when the
level is edited by hand.

## Design notes worth knowing before editing

- **The receive must never be cancelled** on the mod side; see the mod's
  `net/WebSocket`. On this side, a turn is a task that can be cancelled, and the
  tool loop closes its transaction in a shielded cleanup when it is.
- **One transaction per turn, opened lazily.** A read-only turn must not open one
  (it would leave an empty undo step), and an abandoned one breaks every later
  turn with `TRANSACTION_ACTIVE`.
- **Tool arguments are the protocol payload**, with no translation layer, so a
  field name cannot be wrong in two places. The one exception is
  `search_object_catalog`, answered locally.
- **Tool schemas match the mod's validator, not the spec's prose.** In particular
  there are no colour-channel properties; the mod does not accept them.
- **Nothing derives from `hash()`** in the scripted provider: Python randomises
  string hashing per process, so a test asserting on it would be intermittently
  flaky.
