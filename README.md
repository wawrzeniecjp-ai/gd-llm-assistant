# GD AI Assistant

Natural-language editing for the Geometry Dash level editor. You type what you
want, a local sidecar process drives an LLM, and the model edits your level
through the editor's own API — in batches, roughly one undo step per request.

```
GD (Geode mod)  ──ws://127.0.0.1:8765──►  sidecar (Python)  ──https──►  LLM provider
```

The mod is the trust boundary: it validates and rate-limits everything the
sidecar asks for. The sidecar owns the API key and the conversation, and never
touches game memory or the level file.

---

## ⚠️ Read this before you send a message

**Your level contents are sent to a third party.**

When you send a message, the sidecar asks the mod for editor state and object
data — object positions, types, properties, selection, the level name — and
sends that, together with your message, to whichever LLM provider you
configured. That is inherent to how this works: the model cannot edit a level it
cannot see.

What that means in practice:

- **Nothing is sent until you submit a message.** Opening the editor, editing by
  hand, and closing the editor send nothing to a provider.
- **The provider's terms apply to what you send.** Same as pasting your level
  data into a chat window.
- **A secret API key is never sent to the game.** It lives only in the sidecar's
  `.env`, is redacted from logs, and never reaches the mod.
- **The handshake is local.** The mod and sidecar share a token file on this
  machine; the socket is loopback-only by default.
- **Your level file is never touched directly.** Edits go through the editor's API,
  so they are undoable and are lost if you do not save.

If any of that is a problem for a level, do not point this at it.

---

## Requirements

| | |
| --- | --- |
| Geometry Dash | **2.2091** (Windows) |
| Geode | **v6 nightly**, loader commit `57415d9` or later. The v5.11.2 *release* does not support 2.2091 |
| Python | 3.11+, via [`uv`](https://docs.astral.sh/uv/) |
| LLM provider | An Anthropic or OpenAI-compatible API key — or none, using `scripted` |

## Install

1. **Geode.** If Geode is already installed, its resources are in place. Replace
   `Geode.dll`, `XInput1_4.dll` and `GeodeUpdater.exe` in the Geometry Dash folder
   with the v6 nightly build (all three, from one build).
2. **The mod.** Copy `wawrzeniecjp.gd-ai-assistant.geode` into
   `<Geometry Dash>\geode\mods\`.
3. **The sidecar.** Copy this repo somewhere, then run `start-sidecar.bat` once to
   create `sidecar\.env`, and fill it in.

Launch the game **through Steam**. Running `GeometryDash.exe` directly exits
almost immediately.

## Run

1. Double-click `start-sidecar.bat` and leave the window open.
2. Open a level in the editor.
3. Press **Ctrl+Shift+C** to show or hide the assistant panel.
4. Type a message and press **Enter** (or click **Send**).

The header square shows the connection state: grey (disconnected), amber
(reconnecting or needs configuration), green (ready).

## Trying it without an API key

Set `llm_provider=scripted` in `sidecar\.env`. That is a deterministic fake: it
makes real tool calls, so the panel, the tool loop, transactions and undo all
work for real — but it places a small fixed demo edit rather than reading and
editing your actual level. Useful for checking the installation.

## What it can do

The model has tools to read the level and to create, modify, delete and select
objects. It cannot save, upload, play, or open the level file, and it cannot
touch trigger-specific properties.

One request is grouped into one transaction, so `Ctrl+Z` should undo the whole
request at once.

## Known limitations

- **The object catalog is a small seed.** The model looks up object ids through a
  catalog, deliberately tiny because an unverified id would silently place the
  wrong object. Add verified ids via `catalog_path` (see `sidecar\.env.example`).
- **Markdown is not rendered.** Messages are plain text, with fenced-code markers
  stripped, so a reply reads as prose rather than styled text.
- **The mouse wheel does not scroll the transcript.** Click-and-drag works.
- **Conversation history lives in the sidecar's memory**, keyed by level name. It
  survives closing and reopening the editor, and is lost when the sidecar
  restarts. Nothing is stored in the level file.
- **The assistant is not told when you edit by hand.** If you change the level
  outside the assistant, it may reason from what it last saw. Ask it to re-check.
- **No Revert button on the summary yet.** Use `Ctrl+Z` in the editor.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Game exits a second after launch | Launched directly instead of through Steam |
| No Geode button in the menu | Geode's UI is unavailable on a mismatched game build — check the log line `Running Geode v…` |
| Panel says it cannot reach the sidecar | `start-sidecar.bat` is not running, or it failed to bind port 8765 |
| Panel says the sidecar needs configuration | `llm_provider`, `api_key` or `model` is unset in `sidecar\.env` |
| Mod does not load at all | Game/Geode version mismatch. The mod targets **2.2091** |
| Panel never appears | Not in the level editor — the panel is editor-only |

Logs: `<Geometry Dash>\geode\logs\` for the mod, and the `start-sidecar.bat`
window for the sidecar.

## Repository layout

| Path | What it is |
| --- | --- |
| `src/` | The Geode mod (C++23) |
| `sidecar/` | The Python sidecar: provider adapters, tool loop, catalog |
| `protocol/v1/` | JSON Schemas — the contract both sides validate against |
| `SPEC.md` | The specification, decision log and measured findings |
| `docs/LEVEL-DESIGN-SPEC.md` | The level design layer: GridText, simulator, music, plan, patterns, decoration, effects, critique, evals |
| `docs/UPGRADING.md` | Checklist for a new GD or Geode version: what was measured, how to re-measure it, how to review the role-table diff |

The protocol is the test seam: the mod cannot run headless, so both sides are
validated against the schemas in `protocol/v1/` instead.
