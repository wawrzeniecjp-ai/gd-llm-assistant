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

Scaffold. Implemented: configuration, protocol schema loading and validation,
token file handling, log redaction, and the connection lifecycle through the
handshake (single client, `Origin` rejection, token check, version check,
`ping`, `editor_closed`).

Not implemented yet (Phase 4): provider adapters, the tool loop, transactions,
editor requests, session store, catalog.
