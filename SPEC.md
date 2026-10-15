# Geometry Dash LLM Assistant Mod — Full Specification

# 0. Document Overview

| Field | Value |
| --- | --- |
| Project name | GD AI Assistant |
| Document version | 0.10.0 |
| Status | Draft — Phases 0-4 implemented. **In-editor testing blocked by the GD 2.209 update; see 0.1.** |
| Target platform | Windows only (initial release) |
| Target Geode version | v5.11.x (Geode SDK 5.11.1 verified locally; requires C++23). Geode v6 is WIP — see §15 Q3 |
| Target GD version | 2.2081 |

## 0.1 Known blocker: GD 2.209 breaks the toolchain (2026-10-16)

**In-editor testing is blocked. The sidecar is not, and neither is development.**

Steam pushed Geometry Dash 2.209 on 2026-10-16 at 00:11. The game now crashes during
Geode's startup, before any mod loads, so nothing can be tested in the editor.

Measured, from the Geode log and the crash dump:

```
Running Geode v5.11.1 in Geometry Dash v1791899187 (forward compatibility mode)
Faulty Module: libcocos2d.dll
Exception Code: c0000005 (EXCEPTION_ACCESS_VIOLATION)
Exception Details: Failed to read from memory at 0xFFFFFFFFFFFFFFFF
  libcocos2d.dll + 0xdd68f
  libcocos2d.dll + 0x7a70c (cocos2d::CCEGLView::onGLFWWindowFocus + 0x4c)
  Geode.dll + 0x16133c (GLFWFocusCallback | loader/src/platform/windows/input.cpp:693)
```

- The reported game version is the nonsense `v1791899187` (an uninitialised read):
  Geode's version probe does not understand the new binary.
- Geode falls back to **forward compatibility mode**, printing ten `[Forward Compat]`
  degradations, and then faults in its own GLFW focus shim.
- **This is Geode core, not this mod.** The faulty module is `libcocos2d.dll` and the
  stack is entirely Geode's own input handling. The crash log records `Failed to load: 0`
  -- all 12 mods loaded; the game died afterwards.
- Geode's own update check reports 5.11.1 as the latest, so **no fixed loader exists yet**.

### What this does and does not block

| Blocked | Still available |
| --- | --- |
| In-editor verification of anything | All sidecar work: the tool loop, providers, catalog, session store |
| Acceptance criteria A1, A2, A4 (frame budget, undo grouping, soak) | The whole test suite, which runs headless |
| Phase 5 end-to-end and soak testing | Documentation, packaging and the launcher script |

This is survivable in large part because of a decision already taken: SPEC.md 13 makes
**the protocol the test seam**, on the grounds that a Geode mod cannot run headless in
CI. That prediction has now paid for itself -- 102 sidecar tests cover the turn
lifecycle, the transaction rules and the caps without the game being runnable at all.

The gap it leaves is the one the seam cannot cover: whether the mod's own C++ behaves
correctly against a real editor. That is deferred, not lost.

### Options

1. **Roll Geometry Dash back to 2.2081.** The standard remedy in this scene and the
   only one that restores in-editor testing now. Steam can fetch an older build with the
   console command `download_depot 322170 <depot_id> <manifest_id>`; the manifest id is
   per build and must be looked up for 2.2081 (SteamDB lists them per build). Current
   build for reference: **25909607**. Afterwards, set the game to not auto-update, or
   Steam will repeat this on the next launch.
2. **Wait for Geode.** Nothing to do but wait; the fix is not ours.
3. **Continue on the unblocked work** (see the table above), and re-verify in the editor
   once either 1 or 2 lands.

### Consequence for `mod.json`

The mod declares `"gd": { "win": "2.2081" }`. Do **not** add 2.209 until the game can
actually be launched and the mod verified against it -- a version claim that has never
been run is worse than an honest narrower one, because it turns a clear refusal into a
crash.

## 0.2 Changes from 0.8.0

Phase 3 (chat UI) is implemented and verified end to end in the editor against the
scripted sidecar. This entry records the measured behaviour, because most of it is
not what the source suggests and every item below cost a debugging round.

**Enter key (the hard one).** Three mechanisms were tried and measured not to work
before the fourth did. `geode::TextInput` forwards only `textChanged` to its
delegate, so `enterPressed`/`textInputReturn` are never called. Registering the panel
as a keyboard delegate fails too — with `addDelegate`, `forceAddDelegate`, and
`CCLayer::setKeyboardEnabled(true)`, not one `keyDown` fired while a text field had
focus. `CCTextInputNode::onTextFieldInsertText` fires per character but reports
`KEY_Unknown` every time and never fires for Enter. **What works is a `$modify` hook
on `EditorUI::keyDown`**, which GD demonstrably dispatches (Geode's keybind mod hooks
the same function). This is now a spike-level finding, not an implementation detail.

**Touch priority runs the opposite way to intuition.** Lower values are dispatched
first. A panel that claims a region at a low priority steals the gesture from its own
children: at `-400`/`-499` the panel swallowed scroll drags before the `ScrollLayer`
saw them, which is why the transcript could not be scrolled. `-1` is the value that
works — children get their touches first, and the panel still beats the editor layer
at `0`. Claiming the *whole* panel is separately wrong: it makes the Send button dead.

**Layout scaling silently overrides child scale.** `SimpleAxisLayout`'s main-axis
default is `AxisScaling::ScaleDownGaps`, which scales children and overrides the
child's own `setScale`. Symptom: reducing the font size appeared to do nothing, and
text stayed wider than the panel. Fixed by setting both axes to `AxisScaling::None`.
Relatedly, `AxisDirection::BottomToTop` reverses child order, which made the
transcript read backwards.

**Fonts.** `bigFont.fnt` is size 32 with capital-shaped lowercase glyphs; body text
rendered as an all-caps wall at double height. Messages now use `chatFont.fnt` (size
16, real lowercase, GD's own chat font).

**The `!` key was the character filter, not the font.** `bigFont.fnt` does contain
`!` and `'`; both were verified in the `.fnt`. The actual cause was
`geode::TextInput`'s default filter, fixed with `setCommonFilter(CommonFilter::Any)`,
which also allows the apostrophe — an ordinary English character a chat box cannot
do without.

**A sprite's `setColor` multiplies with its texture.** Tinting a dark frame green
yields black, so the status dot was invisible on a near-black panel. It is now a
`CCLayerColor`, which has no texture to multiply against. Similarly,
`CCSpriteFrameCache::spriteFrameByName` does **not** return null for a missing frame —
it returns a **fallback texture** and logs a warning, so "does this frame exist" needs
to be checked differently.

**Stray instrumentation, adopted as a rule.** The `hello` result arrives *before* the
panel can be built (the bridge connects from `LevelEditorLayer::init`, but the panel
needs `EditorUI`), so a status posted in that window was silently dropped and the
panel opened looking disconnected while turns worked. The host now remembers and
replays. The sidecar's `history` frame lands immediately after the handshake and will
wipe a just-posted status line unless the clear is scoped to the conversation.

**Cancelling a socket read corrupts the stream.** `arc::timeout(...)` around
`receiveText()` discards bytes already consumed from the buffer and can emit a partial
frame during a pong write, desynchronising the peer. The symptom was the sidecar
closing with "keepalive ping timeout" two minutes into every session — which reads as
a network fault and is not one. The receive is now uncancellable, per-frame work moved
to a separate watchdog task, and `arc::TcpStream::shutdown` is used to end a session.

**Sanitising works, and the scripted provider proves it.** The fake provider's replies
deliberately contain `<cr>`, `<c-FF00FF>` and `</c>` so the sanitiser has end-to-end
coverage: the tags are stripped, not rendered.

**Sanitising untrusted text (§8.3.1) is implemented, not just specified** (§8.3.1).
Angle-bracket tags are removed wholesale rather than allow-listed, so a future switch
to a rich-text renderer cannot turn unstripped markup into a formatting instruction.

**Accepted gaps, deliberately.** Markdown is **not** rendered: `MDTextArea` is unused
and fenced-code fence lines are stripped so they are not shown literally. Body text is
plain. The mouse wheel does not scroll the transcript (drag does): GD's
`CCMouseDispatcher` offers no priority API, so the editor keeps the wheel; recorded
rather than worked around. `Home`/`End` and `!`-style navigation keys inside the field
are GD's `CCTextInputNode` behaviour, which implements only left/right/backspace/delete.

## 0.2 Changes from 0.6.0

- **Phase 1 exit criteria met, verified in the editor.** The mod connected on editor open, completed `hello`, answered `ping` and `get_editor_state`, and closed cleanly on editor close. Measured against a real level: 10000 objects, 0 selected, `editor_layer` 0.
- **New trap recorded (§6.1.11):** `LevelEditorLayer` does **not** declare `onExit` in the Geode bindings, unlike `PlayLayer` and most other layers. A `$modify` hook on it compiles (the call resolves to the inherited `CCNode::onExit`) but never fires. This caused a 5-10 second hang on game exit, because the session task kept polling and Geode's async-runtime shutdown waited on it. Shutdown now uses the `Exiting` game event plus editor-presence polling.
- **New protocol invariant (§7.3):** a `response` must never draw a reply, not even an error reply for a malformed one. Replying to a reply lets the two sides echo without bound. Both sides now enforce this; the sidecar checks for `response` *before* envelope validation.
- **`matjson` gotcha:** `matjson::Value()` default-constructs an **object**, not null. Event frames therefore need `matjson::Value(nullptr)`; the wrong form emitted `"id": {}`, which the sidecar rejected as `INVALID_REQUEST`.
- **`ground_y` and `camera` are still omitted** from `get_editor_state` responses: their derivation is unverified, and a wrong `ground_y` would silently misplace generated objects. The schema requires only `object_count`, `grid_size` and `playtesting`.

## 0.2 Changes from 0.5.0

- **S2 resolved — no WebSocket dependency.** The bridge uses a hand-written RFC 6455 client on `arc::TcpStream` (D14, §6.1.10). This satisfies §12's "avoid unmaintained libraries": websocketpp is neglected, and any third-party library would bring its own thread and event loop, which §6.1.3 forbids bridging into Arc.
- **S2 verified on the wire, not just compiled.** A mod load completed the full round trip against the Python sidecar: handshake accepted (`Sec-WebSocket-Accept` independently verified), token accepted, `hello` parsed with the correct mod/GD/Geode versions, and both the response and the pushed `history` frame decoded. The client frames ran on `arc-worker-*` threads, confirming §6.1.3.
- **The one missing primitive was found and supplied.** Geode ships `sha256`/`Sha256Hasher` but **no SHA-1**, which RFC 6455 requires for the handshake. The mod therefore carries its own SHA-1 (§6.1.10), self-tested against the FIPS 180-1 vectors and the RFC 6455 handshake example before any connection is attempted. Framing itself needs no cryptography.
- **New modules:** `src/net/websocket.{hpp,cpp}` (handshake, masked text frames, fragmentation reassembly, ping/pong, close, 4 MB inbound cap) and `src/net/sha1.{hpp,cpp}`. Recorded in §6.1.10 and the §6.1.9 layout.
- **`Origin` omission is load-bearing:** the client deliberately sends no `Origin` header, which is what makes D11's rejection rule enforceable. Now stated explicitly in §6.1.10.

## 0.3 Changes from 0.4.0

- **Version target settled and raised:** Geode SDK **5.11.1** is installed and the target is now **v5.11.x**. Geode v6 exists only as a work-in-progress migration guide, so no v6 work is in scope. Resolves §15 Q3 and Appendix A #1.
- **Toolchain verified end to end:** Clang 23.1.3 (scoop LLVM), Ninja, CMake ≥ 3.29, Git, MSVC 14.51 + Windows SDK 10.0.26100 (VS BuildTools), Geode CLI 3.9.0, and `GEODE_SDK` set per workspace. Recorded in the new §6.1.9.
- **Third-party refresh dependency removed:** the editor exposes no generic `update()`; vanilla `updateObjectLabel` / `updateObjectSection` / `quickUpdateAllPositions` / `updateGridLayer` / `updateGameObjects` replace `nwo5::editor::update()`, so the "Silly API" is no longer a dependency (§6.1.6, Appendix A #10).
- **Undo grouping promoted to the primary path:** `addToUndoList(UndoObject*, bool keepRedo)`, `addToRedoList`, `handleAction`, `m_undoObjects` / `m_redoObjects` and `m_increaseMaxUndoRedo` make a grouped mixed-mutation undo entry feasible. The bookkeeping fallback is retained, but is no longer the assumed mechanism (§6.1.5, S3, Appendix A #4).
- **Text rendering revised:** `geode::Label` is quad-based with Unicode and emoji support and rich-text colour tags, so the bitmap-font glyph-coverage spike is retired (§8.3.1, §8.9, Appendix A #6).
- **WebSocket client narrowed:** Geode's `web::WebRequest` is HTTP-only (curl_multi); the SDK provides no WebSocket client, so a third-party library or manual framing over `arc::TcpStream` is required (§6.1.2, Appendix A #3).
- **Keybinds resolved:** keybinds are core Geode in v5 (`geode::Keybind`) — not an external dependency (§6.1.2, Appendix A #12).
- **Job-completion primitive confirmed:** Arc provides `arc::oneshot`, `arc::mpsc`, `arc::Semaphore` and `arc::Notify`; `arc::Mutex` is the async-aware lock (§6.1.4, Appendix A #9).
- **New build-configuration section (§6.1.9):** Arc ships only its Core and Time features by default, so the mod's `CMakeLists.txt` must opt into the rest; the expected repository layout is specified there.

## 0.4 Changes from 0.3.0

- **Topology flipped (proposed):** the mod is the WebSocket client; the sidecar is the server. Resolves the port/role contradictions in 0.3.0 (§5, D1).
- **Corrected API claims:** `createObject` / `removeObject` take `noUndo` (0.3.0 had it inverted); v4 coroutine/`Popup<>` APIs replaced with v5 equivalents (§6.1).
- **Protocol rewritten:** added handshake, auth, chat channel, transactions, uniform response envelope, property schema, limits, new error codes (§7).
- **Execution model:** time-sliced main-thread job queue, per-turn undo groups, coalesced events (§6.1.4–6.1.6).
- **UI:** non-modal panel replaces `geode::Popup`; units in points; glyph and sanitisation rules; Stop button; setup states (§8, §9).
- **Security:** token + Origin check, prompt-injection and data-disclosure sections (§12).
- **New sections:** goals/non-goals (§3), decision log (§5.3), testing strategy (§13), phase-0 spikes with exit criteria (§14), verification checklist (Appendix A).
- Markdown/table formatting defects fixed.

Items marked **(verify)** are assumptions that must be confirmed by a phase-0 spike or against current docs. Appendix A lists them.

# 1. Problem Statement

There are two primary approaches to bringing LLM-powered assistance to Geometry Dash level editing:

**Direct GMD file editing** — simpler to implement, but requires a slow, manual import/export cycle between the LLM tool and the game. There is no immediate visual feedback, making iteration painful and disengaging.

**In-game assistant via Geode** — requires a more complex architecture (WebSocket bridge, sidecar server), but provides instant visual feedback. The user sees objects appear, move, and change in real time as the LLM reasons and acts. This is significantly more engaging and accelerates the understand–iterate loop.

This project implements approach 2.

# 2. Intended Users

- Players who are already comfortable installing and managing Geode mods.
- Users who have a basic understanding of running a local companion application (e.g., a Python script).
- Not targeting users who expect a zero-setup, single-binary experience — the sidecar architecture is a deliberate trade-off for flexibility and security.

# 3. Goals and Non-Goals

## 3.1 Goals (v1)

1. Natural-language editing of **non-trigger objects** in the level editor: create, move/transform, recolour (channel assignment), group assignment, delete, select, and describe.
2. Live, visible edits with **one undo step per assistant turn**.
3. LLM API key never present in the game process.
4. No game freezes: no frame stall attributable to the mod exceeds the budget in §3.3.
5. Safe by construction: every command is validated and bounded in the mod, regardless of what the sidecar sends.

## 3.2 Non-Goals (v1)

- Trigger-specific properties (target groups, move deltas, durations, etc.). Trigger objects can be created with common properties only; full trigger support is a v1.1 item (§14, Q-new-2).
- Verifying that a level is beatable, playtest control, or gameplay analysis.
- Guaranteeing good level *design*; the assistant executes edits, quality depends on the model and prompt.
- Auto-launching or managing the sidecar from inside the mod.
- Multi-user / network-accessible operation. Localhost only.
- Platforms other than Windows (architecture is portable; editor bindings and testing are Windows-first).
- Saving the level: the user saves via the normal editor flow. The assistant never saves.

## 3.3 Acceptance Criteria (measurable)

| # | Criterion |
| --- | --- |
| A1 | A 1000-object `create_objects` request completes with no single frame spending more than the frame budget (default 4 ms) in mod code. |
| A2 | One Ctrl+Z in the editor reverts an entire assistant turn that created, modified, or deleted up to 5000 objects (verify feasibility, §14 spike S3). |
| A3 | Closing the editor mid-turn, disconnecting the sidecar mid-turn, and cancelling mid-turn all leave the editor in a consistent state with no crash. |
| A4 | 100 consecutive scripted turns (fake LLM) with random user edits interleaved produce no crash and no orphaned transactions. |
| A5 | Connections without the correct token, or carrying an `Origin` header, are rejected by the sidecar. |
| A6 | With no API key configured, the panel shows a setup message and no request reaches an LLM provider. |

# 4. Core Constraints

| Constraint | Detail |
| --- | --- |
| Platform | Windows only (initial release). |
| Non-blocking | The mod must never block the game's main thread on I/O. Network I/O runs off the main thread; all Cocos2d/GD API calls run on the main thread, in a time-budgeted job queue (§6.1.4). |
| Key isolation | The LLM API key never enters the game process. It lives in the sidecar's configuration only. Mod settings do not store or expose it. |
| C++ standard | Geode v5 requires C++23. |
| Async model | Geode v5 replaces the old `Task` system with an Arc-based async runtime using C++ coroutines (`async::spawn`, `arc::Future`, `async::waitForMainThread`). |
| Trust boundary | The mod is the trust boundary. It validates and rate-limits everything the sidecar sends. The sidecar is not trusted to enforce limits. |

# 5. System Architecture

Two processes communicate over a local WebSocket connection. **The sidecar is the WebSocket server; the mod is the client** (decision D1, proposed).

```text
┌─────────────────────────────────────────────────────────────┐
│  Geometry Dash Process                                      │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Geode Mod (.geode)                                   │  │
│  │                                                       │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐   │  │
│  │  │ Chat Panel  │  │ WS Client    │  │ Command     │   │  │
│  │  │ (UI Layer)  │  │ (async rt.)  │  │ Executor    │   │  │
│  │  └──────┬──────┘  └──────┬───────┘  └──────┬──────┘   │  │
│  │         │                │                  │         │  │
│  │         │         ┌──────▼──────────────────▼──────┐  │  │
│  │         └────────►│ Main-Thread Job Queue          │  │  │
│  │                   │ (time-budgeted, per frame)     │  │  │
│  │                   └────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────┘  │
│                            │                                │
└────────────────────────────┼────────────────────────────────┘
                             │  ws://127.0.0.1:8765
                             │  (token handshake, no Origin)
┌────────────────────────────▼────────────────────────────────┐
│  Sidecar Process (Python)                                   │
│                                                             │
│  ┌─────────────────┐  ┌──────────────┐  ┌───────────────┐   │
│  │ WS Server       │  │ LLM Provider │  │ Tool Loop /   │   │
│  │ (auth, 1 client)│  │ Adapter      │  │ Executor      │   │
│  └─────────────────┘  └──────────────┘  └───────────────┘   │
│                                                             │
│  ┌─────────────────┐  ┌──────────────┐  ┌───────────────┐   │
│  │ Session Store   │  │ Action Log   │  │ Config Store  │   │
│  └─────────────────┘  └──────────────┘  └───────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

Rationale for mod-as-client: the sidecar is long-lived and owns the stable endpoint; reconnect/backoff lives in the mod next to the editor lifecycle; the game process opens no listening socket; and port collisions (e.g., with WSLiveEditor on 1313, or two GD instances) are handled on the sidecar side. Cost: the sidecar must be running before the editor connects; the UI makes this state explicit (§8.8).

## 5.1 Process Responsibilities

| Process | Owns | Never touches |
| --- | --- | --- |
| Geode Mod | UI rendering, user input capture, WebSocket client, command validation and limits, editor object CRUD, transactions/undo grouping, editor state queries, change events | LLM logic, API keys, conversation history |
| Sidecar | WebSocket server and auth, LLM conversation, tool loop, session persistence, API key storage, action logging, object-ID catalog | Game memory, Cocos2d/Geode APIs, direct level file access |

## 5.2 Turn Flow

```text
User types message
  → mod: user_message                                  (event)
  → sidecar: turn_started                              (event)
  → sidecar runs LLM + tool loop (≤ max_tool_rounds):
        get_editor_state / query_objects / summarize_region     (requests)
        begin_transaction                                       (lazily, before first mutation)
        create_objects / modify_objects / delete_objects        (requests)
        end_transaction                                         (always, at turn end)
  → sidecar: assistant_message                         (event)
  → sidecar: turn_finished { status, actions, txn_id } (event)
  → mod renders summary bubble with "Revert" action
```

## 5.3 Decision Log

All entries are **Proposed** until the owner confirms. Each replaces or answers an item from 0.3.0.

| ID | Decision | Replaces / answers |
| --- | --- | --- |
| D1 | Mod is WS client, sidecar is server on `127.0.0.1:8765`. | Port/role contradictions; Q2 partly |
| D2 | One undo group per assistant turn via transactions (§6.1.5). | Per-op undo flag; Q3 |
| D3 | Raw `undo`/`redo` are not exposed to the LLM. User reverts via the editor or the "Revert" action on the turn summary. | Q3 |
| D4 | Sidecar is stateless w.r.t. level contents: query on demand, no mirror. | Q4, §5.2.4 |
| D5 | Native function-calling APIs, not text parsing. | §5.2.3 |
| D6 | No sidecar auto-start in v1; ship a launcher script. | Q2 |
| D7 | Sidecar in Python (`uv run`, `websockets`, provider SDKs). | Q1 |
| D8 | Custom non-modal panel instead of `geode::Popup`. | §7.1 |
| D9 | Object identity = integer unique ID (`uniqueID`), resolved per command on the main thread. | §6 `string` ids |
| D10 | All limits/rate limits enforced in the mod. | §10 rate-limit row |
| D11 | Shared-token handshake plus rejection of any connection carrying an `Origin` header. | §10 "malicious clients" row |
| D12 | Multi-turn tool loop is core, capped by `max_tool_rounds`. | Q5 |
| D13 | Editor closed mid-flight: fail in-flight requests with `EDITOR_NOT_OPEN`, drop queue, end/abandon transaction, sidecar marks turn `incomplete`. | Q6 |
| D14 | Bridge transport is a hand-written RFC 6455 client on `arc::TcpStream`, with a bundled SHA-1 for the handshake. No WebSocket dependency. | S2, Appendix A #3 |

# 6. Component Specifications

## 6.1 The Geode Mod (C++)

### 6.1.1 Responsibility

UI rendering, capturing user input, managing the WebSocket client connection, validating and executing editor commands on the main thread within a time budget, grouping edits into transactions, and emitting coalesced change events.

### 6.1.2 Technical Stack

| Concern | Choice | Rationale |
| --- | --- | --- |
| UI container | Custom non-modal `CCNode`/`CCLayer` panel attached to the editor UI layer (verify attachment point) | `geode::Popup` derives from the alert-layer family and is, I believe, modal with a dimming overlay; that blocks the editor and defeats live visual feedback (verify). |
| Message rendering | `MDTextArea` for assistant/system text; sanitised input only (§9.3) | Supports bold, italic, monospace via md4c. |
| Input | `geode::TextInput` | Standard Geode component. |
| WebSocket client | **Decided (D14, §6.1.10):** hand-written RFC 6455 client on `arc::TcpStream`. Geode's `web::WebRequest` is HTTP-only and the SDK ships no WebSocket client, so this replaces the third-party option | No new dependency, and it runs natively on the Arc runtime, so there is no foreign thread or event loop to bridge into the main thread (§6.1.3). websocketpp is unmaintained; a self-contained library would still add a second concurrency model. |
| Async | v5: `async::spawn`, `arc::Future<>`, `co_await async::waitForMainThread<T>(fn)`; async-aware synchronisation via `arc::Mutex`, `arc::Notify`, `arc::oneshot`, `arc::mpsc` | Replaces v4 `coro::spawn` / `$async`. Never hold a `std::mutex` across a suspension point. |
| Keybinds | `geode::Keybind` — core Geode in v5, not an external dependency | Rebindable through Geode's keybind UI; avoids hard-coded conflicts. |

### 6.1.3 Threading Rules

Hard requirements:

1. Network I/O runs on the async runtime (or the library's own thread). It must never touch Cocos2d or GD objects.
2. All editor reads and writes execute on the main thread. In v5, spawned futures run on the async runtime; only callback overloads and `waitForMainThread` execute on the main thread. Do not assume a coroutine body is on the main thread.
3. The main thread never awaits network I/O or blocks on a lock held by the network side.
4. Cross-thread notifications use thread-safe event variants. Event listeners run on the thread that dispatched the event.
5. Never hold a `GameObject*` (or any Cocos2d node pointer) across a frame boundary or an `await`. Re-resolve by unique ID at the start of each job slice.

### 6.1.4 Main-Thread Job Queue and Time Slicing

"Never block the main thread" is not satisfied by dispatching a large batch into a single `waitForMainThread` callback. Instead:

- The network side validates a request, then enqueues a **job** (thread-safe queue) and awaits a completion signal — `arc::oneshot` for a single completion, `arc::mpsc` for progress, `arc::Notify` for wake-ups (all confirmed available, Appendix A #9).
- A per-frame scheduled update on the main thread drains the queue. Each job is resumable: it processes items until the frame budget (`frame-budget-ms`, default 4 ms) is exhausted, then yields to the next frame.
- The response is sent when the job completes. Optional `progress` events may be emitted for jobs spanning more than ~10 frames.
- Validation (types, ranges, ID checks, limits) happens **before** any mutation so a request is rejected whole rather than half-applied.
- Cancellation: a cancelled or disconnected job stops at the next slice boundary; items already applied remain inside the open transaction (and are revertible).
- Limits (all enforced here, all configurable within hard maxima):

| Limit | Default | Hard max |
| --- | --- | --- |
| Objects per `create_objects` / `modify_objects` / `delete_objects` request | 1000 | 5000 |
| Objects mutated per transaction (turn) | 5000 | 20000 |
| Inbound message size | 1 MB | 4 MB |
| Query result objects per page | 100 | 500 |
| Queued jobs | 16 | 64 |
| Request timeout (mod → waiting for sidecar) | 30 s | 120 s |

Exceeding a limit returns `LIMIT_EXCEEDED` or `PAYLOAD_TOO_LARGE` with the limit value in the error details.

### 6.1.5 Transactions and Undo Grouping

Goal: one user Ctrl+Z reverts a whole assistant turn.

- `begin_transaction` opens a transaction; only one may be open at a time (`TRANSACTION_ACTIVE` otherwise). All mutations until `end_transaction` are grouped.
- Mechanism (**primary path, S3**): group the editor's undo entries so that a single undo step reverses the whole transaction. `LevelEditorLayer` exposes the whole surface needed — `addToUndoList(UndoObject*, bool keepRedo)`, `addToRedoList(UndoObject*)`, `handleAction(bool undo, CCArray* undoObjects)`, the `m_undoObjects` / `m_redoObjects` lists, `m_increaseMaxUndoRedo` (raise it above the 5000-object A2 target) — plus the `UndoObject` class. Mutations run with `noUndo = true` and their `UndoObject`s are supplied explicitly, so one grouped entry covers a mixed create/modify/delete transaction. Residual uncertainty: whether a single `UndoObject` may hold entries of more than one kind; if not, push N entries inside one `handleAction` call so the undo stack still advances exactly once. Confirm in S3.
- **Fallback if grouping is infeasible:** the mod records, per transaction, the created object IDs, removed object snapshots (serialised via the object-string mechanism), and modified-object prior property sets, and implements `revert_transaction` from this bookkeeping with the editor's own undo list left untouched. A2 then applies to the "Revert" action, not Ctrl+Z.
- `revert_transaction { txn_id }` succeeds only if the transaction is the most recent change (no user edits after it). Otherwise → `TRANSACTION_NOT_REVERTIBLE`, and the UI tells the user to use the editor's undo.
- A transaction left open when the connection drops or the editor closes is ended automatically by the mod.
- After any revert the mod sends `transaction_reverted` so the sidecar can tell the LLM on the next turn that its changes were reverted.
- Mutations outside a transaction are permitted and form their own single undo entry.

#### 6.1.5.1 Removal and the undo stack (measured, 0.8.0)

Removal has a constraint that creation does not, and it is not documented anywhere in the API:

- **`removeObject(object, noUndo)` must be called with `noUndo = false`.** Passing `true` corrupts the heap — the game dies with `STATUS_HEAP_CORRUPTION` (0xc0000374), raised from ntdll. This was reproduced on objects the mod created *and* on objects already in the level, so it is not about the object's provenance. The asymmetry with creation is notable: `createObject(..., noUndo = true)` is fine. The flag evidently does more on the removal path than suppress an undo entry.
- Rather than spend more cycles on it, note what made it hard: four other explanations were tested and falsified first (object coordinates, which removal API is used, selection involvement, and the calling context), and the "known-good" reference used as a control turned out not to be comparable — it only ever ran during editor init.

**Consequence, accepted for now:** removals land on the user's undo stack while creations do not. Ctrl+Z can therefore undo an assistant deletion but not an assistant creation, and D2's "one undo group per turn" currently holds only for creates. Grouping the removals per transaction is possible (`UndoCommand::DeleteMulti` exists) and is a v1.1 candidate. This is recorded rather than fixed: it is a consistency wart, not a correctness problem, and the phase's real risk was whether the operations worked at all.

### 6.1.6 Editor API Reference

Verified against the Geode bindings for `LevelEditorLayer`:

| Operation | API | Notes |
| --- | --- | --- |
| Create object | `LevelEditorLayer::get()->createObject(int key, CCPoint position, bool noUndo)` | **`noUndo = true` disables undo.** Pass `false` for undoable creation (or `true` inside the fallback bookkeeping scheme). Returns `GameObject*` (check for null). |
| Delete object | `LevelEditorLayer::get()->removeObject(GameObject*, bool noUndo)` | Same flag semantics. |
| Find by unique ID | `LevelEditorLayer::get()->findGameObject(int uniqueID)` | Basis of D9. Confirm unique ID stability across undo/redo (verify, S7). |
| Enumerate | `getAllObjects()` (`CCArray*`, backed by `GJBaseGameLayer::m_objects`) | **Resolved in 0.7.1 by measurement.** Do **not** use `LevelEditorLayer::m_objectVector`: on a level with a handful of objects it reported 10000 entries, all null, so it is a pre-sized table, not the object collection. It also reported the same 10000 on an empty level. Triggers *are* included in `getAllObjects()`. |
| Range query | `objectsInRect(CCRect, bool ignoreGroups)`, `objectsAtPosition(CCPoint)` | Use for `x_range`/`y_range` filters. |
| Undo / redo | `undoLastAction()`, `redoLastAction()` | Not exposed to the LLM (D3). |
| Undo grouping | `addToUndoList(UndoObject*, bool keepRedo)`, `addToRedoList(UndoObject*)`, `handleAction(bool undo, CCArray* undoObjects)`, `m_undoObjects`, `m_redoObjects`, `m_increaseMaxUndoRedo`, class `UndoObject` | Basis of the §6.1.5 primary path (S3). Raise `m_increaseMaxUndoRedo` for the 5000-object A2 target. |
| Batch from string | `createObjectsFromString(gd::string const&, bool noUndo, bool noLimit)` | Candidate fast path for batches; has an object-limit flag, so a limit exists (verify value). |
| Snapshot / re-create | `getLevelString()` | Whole-level serialisation. Basis of the §6.1.5 fallback bookkeeping. |
| Allocate free IDs | `getNextFreeGroupID(excludes)`, `getNextFreeEditorLayer(excludes)`, `getNextFreeOrderChannel(excludes)`, `getNextFreeItemID(excludes)`, `getNextColorChannel()` (all take `std::unordered_set<int> const&` except the last) | Use these instead of inventing group / editor-layer IDs for `create_objects`. |
| Editor layer / UI | `LevelEditorLayer::get()`, `m_editorUI` | |
| Refresh UI after programmatic change | `updateObjectLabel(GameObject*)` (static), `updateObjectSection(GameObject*)`, `quickUpdateAllPositions()`, `updateGridLayer()`, `updateGameObjects()` | Vanilla, so the third-party "Silly API" (`nwo5::editor::update()`) is **not** required. Call the minimal set per slice and measure the cost (A1). |

Important: do not call `EditorUI::createObject` directly (access violation). Use `LevelEditorLayer::get()->createObject()`.

### 6.1.7 Change Events (Mod → Sidecar)

Detecting user and assistant edits requires hooks on the relevant editor functions (which ones: spike S4). Events are **coalesced**, never per-object:

- `level_changed` flushes at most every 250 ms with counts, a bounding box, and `source` (`"user"` or `"assistant"`). Edits made by the mod's own command executor are tagged `assistant`, which prevents echo loops.
- `selection_changed` is throttled to 250 ms and capped at 200 IDs.
- Events are suppressed during playtest and while a job is mid-slice for the same source, then flushed once.

### 6.1.8 Mod Settings (mod.json)

```json
{
  "settings": {
    "sidecar-url": {
      "type": "string",
      "name": "Sidecar WebSocket URL",
      "description": "WebSocket URL of the local sidecar. Must be a loopback address.",
      "default": "ws://127.0.0.1:8765"
    },
    "auto-connect": {
      "type": "bool",
      "name": "Auto-connect on editor open",
      "description": "Connect to the sidecar automatically when entering the editor, and reconnect with backoff if the connection drops.",
      "default": true
    },
    "max-message-length": {
      "type": "int",
      "name": "Maximum message length",
      "description": "Maximum character count for a single user message.",
      "default": 2000,
      "min": 100,
      "max": 10000
    },
    "frame-budget-ms": {
      "type": "int",
      "name": "Per-frame edit budget (ms)",
      "description": "Maximum main-thread time per frame spent applying assistant edits.",
      "default": 4,
      "min": 1,
      "max": 16
    },
    "max-objects-per-request": {
      "type": "int",
      "name": "Max objects per request",
      "description": "Upper bound on objects created, modified, or deleted by a single request.",
      "default": 1000,
      "min": 10,
      "max": 5000
    }
  }
}
```

Notes:

- The model is chosen in the sidecar only. The 0.3.0 `default-model` mod setting is removed (two sources of truth, and no protocol message used it).
- **The API key is not a mod setting.** The mod never receives, stores, or transmits it.
- Token location (§7.2) is a fixed path by default; an optional override setting may be added if needed.

### 6.1.9 Build Configuration (verified)

Confirmed on the development host (Windows, 2026-10-15):

| Tool | Value | Note |
| --- | --- | --- |
| Geode SDK | 5.11.1, `GEODE_SDK` set per workspace | Pin the SDK version; set via workspace settings rather than a global env var |
| Geode CLI | 3.9.0 (scoop `geode-sdk-cli`) | `geode sdk install`, `geode profile add` |
| Loader | 5.9.0 (installed in Steam GD) | Target GD 2.2081 |
| Clang | 23.1.3 (scoop `llvm`), incl. `clang-cl` | MSVC CRT and Windows SDK come from VS BuildTools |
| MSVC + Windows SDK | 14.51.36231, SDK 10.0.26100 (VS 18 BuildTools) | Geode requires VS 2022+; this is **newer** than the tested matrix, so S1 confirms it compiles before any other work |
| CMake | ≥ 3.29 required, must be on PATH | Install to PATH rather than relying on the copy bundled with VS BuildTools |
| Ninja | scoop `ninja` | Geode's generator on Windows |
| Git | required by the SDK's populate steps | |

**Arc feature flags.** Mods include only Arc's Core and Time features by default. The mod's `CMakeLists.txt` must opt into the rest *before* the SDK is added:

```cmake
set(ARC_FEATURE_FULL ON CACHE BOOL "" FORCE)
# or granularly: ARC_FEATURE_NET, ARC_FEATURE_TIME, ARC_FEATURE_SIGNAL, ARC_FEATURE_DEBUG

add_subdirectory($ENV{GEODE_SDK} ${CMAKE_CURRENT_BINARY_DIR}/geode)
```

The network-capable Arc primitives (`arc::TcpStream`, `arc::UdpSocket`) require `ARC_FEATURE_NET`, which matters if S2 selects manual WebSocket framing (§6.1.2).

**`mod.json` gotchas (cost a build cycle each, verified).**

- The `geode` key must **not** carry a `v` prefix. `setup_geode_mod` compares major.minor via `string(REGEX REPLACE "([0-9]+\\.[0-9]+)\\.[0-9]+" ...)`, which is unanchored — so `v5.11.1` yields `v5.11` against the SDK's `5.11` and the configure step fails with a misleading "change the Geode version" error. Use `"geode": "5.11.1"`. The SDK's own reference manifest uses `@GEODE_VERSION_FULL@`, which expands without a prefix; the docs example showing `"v1.0.0"` is wrong for current SDK versions.
- The `gd` value is checked against `GEODE_GD_VERSION` (2.2081) exactly, or accepts `*`.
- `setup_geode_mod` reads `mod.json` from `CMAKE_CURRENT_SOURCE_DIR`, which is why the mod project must stay at the repository root (§6.1.9, repository layout).
- `geode sdk install-binaries` is required before the first configure; without it the SDK fails at `Geode/CMakeLists.txt` with "No valid loader binary to link to".
- `LevelEditorLayer` has no bindable `onExit`; see §6.1.11 before hooking any lifecycle function.

**Repository layout.** No repository existed before this revision; the following top-level layout is a prerequisite for Phase 1:

```text
CMakeLists.txt        mod build (Arc flags above)
mod.json              mod manifest and §6.1.8 settings
src/                  mod C++ sources
src/net/              RFC 6455 WebSocket client and SHA-1 — §6.1.10
protocol/             JSON Schemas — the §13 test seam, shared with the sidecar
sidecar/              uv project: package, tests and test harnesses
tests/fixtures/       recorded turns and fuzz corpora (language-agnostic)
```

All Python test code lives under `sidecar/` — the fake mod, the fake sidecar and
the schema conformance tests — because the mod cannot run headless in CI and
every harness that exercises the protocol is therefore Python. The top-level
`tests/` directory holds only language-agnostic fixtures, so the repository
needs a single Python project rather than two.

The `protocol/` schemas are authored before either side's transport work, because §13 makes the protocol the test seam and both the fake mod and the fake sidecar validate against it. The mod project stays at the repository root because `setup_geode_mod` reads `mod.json` from `CMAKE_CURRENT_SOURCE_DIR`.

### 6.1.10 Bridge Transport (S2, resolved)

Decision D14: the WebSocket client is hand-written against RFC 6455, on `arc::TcpStream`. Implementation: `src/net/websocket.{hpp,cpp}`, with `src/net/sha1.{hpp,cpp}` for the handshake digest.

**Why not a library.** Geode's `web::WebRequest` is HTTP-only (curl_multi) and the SDK ships no WebSocket client, so a dependency was the obvious alternative. It is rejected because websocketpp is effectively unmaintained (§12: "avoid unmaintained libraries where an alternative exists"), and because any third-party library brings its own thread and event loop — which §6.1.3 rule 1 forbids bridging into Cocos2d, leaving a second concurrency model to reconcile with Arc. The Arc-native client has nothing to bridge.

**Why SHA-1 is bundled.** RFC 6455 §4.2.2 requires `base64(SHA1(key + GUID))` for `Sec-WebSocket-Accept`. Geode provides `sha256`/`Sha256Hasher` but no SHA-1, and pulling a crypto library in for one hash is not worth it. SHA-1 is collision-broken, which is immaterial here: RFC 6455 uses it as a handshake checksum proving the peer speaks WebSocket, not as a security control. Framing uses no cryptography at all. `sha1SelfTest()` validates the implementation against the FIPS 180-1 vectors and the RFC 6455 handshake example, and the mod refuses to connect if it fails.

**Deliberate scope limits.** One connection; `ws://` only (no TLS); text frames carrying UTF-8 JSON; no extensions, no permessage-deflate, no subprotocols. This matches what the bridge needs and keeps the frame codec small enough to fuzz (§13).

**Security properties this establishes.**

- No `Origin` header is ever sent, which is what makes D11's "reject any upgrade carrying `Origin`" rule enforceable: the mod and a browser are distinguishable.
- `Sec-WebSocket-Accept` is verified, so an incorrect digest or base64 fails loudly at the handshake rather than causing confusing framing errors later. A cross-implementation check: the Python `websockets` server computes the expected value independently.
- The 4 MB inbound cap (hard max, §6.1.4) is enforced in the mod. An oversized frame length is refused rather than buffered.
- Server frames must not be masked (RFC 6455 §5.1); a masked server frame is rejected as a protocol error.
- Length fields are validated before any read, so a malformed header cannot cause an over-read.

**Threading.** Every method is a coroutine awaited on the async runtime; nothing in `src/net/` touches Cocos2d or GD objects (§6.1.3). Verified during the spike: the round trip ran on `arc-worker-*` threads, not the main thread.

**Open follow-ups** (Phase 1, not blockers): reconnection with exponential backoff (§7.8 step 5), the `hello` retry path on token mismatch (4001), and coverage for the fragmentation and oversized-frame paths, which the spike did not exercise.

### 6.1.11 Editor Lifecycle Hooks (verified)

How the bridge tracks the editor, and the trap that cost a 5-10 second hang on game exit.

**Connecting** is straightforward: `$modify(LevelEditorLayer)` and start the bridge from `init`, gated on the `auto-connect` setting (§6.1.8).

**Disconnecting is not.** `LevelEditorLayer` does **not** declare `onExit` in the Geode bindings — unlike `PlayLayer`, `GauntletSelectLayer`, `GJShopLayer` and many others, which do. A `$modify` hook on `LevelEditorLayer::onExit` therefore compiles, because the call resolves to the inherited `cocos2d::CCNode::onExit`, but the hook is never installed and never fires. There is no compile-time or run-time indication of this.

The consequence was that the session task never observed a stop request, kept polling indefinitely, and Geode's async-runtime shutdown ([`loader/src/main.cpp`](https://github.com/geode-sdk/geode)) blocked waiting on that task — which the user experienced as a 5-10 second hang on exit. Nothing in the log said so; the only symptom was a silent gap between the last mod log line and `Shutting down logger and async runtime..`.

Two mechanisms replace it:

1. **Editor close is polled.** Each 250 ms idle tick hops to the main thread and checks `LevelEditorLayer::get()`, which is null once the editor is gone. Detecting it sends `editor_closed`, closes the socket, and ends the run loop — so leaving the editor does not trigger a pointless reconnect.
2. **Game exit uses `$on_game(Exiting)`.** `GameEventType::Exiting` is documented as firing when the game is about to exit, before static destructors, which is the reliable signal. It sets a flag that both stops the loop and suppresses the main-thread hop, since nothing may service such a callback during teardown.

The poll's failure path matters too: if the main-thread dispatch cannot be scheduled at all, the loop ends the session rather than continuing without a way to observe the editor.

**Rule for future hooks:** before relying on a `$modify` hook for a lifecycle function, confirm the function is declared on that specific class in the bindings, not merely inherited. `grep` the generated bindings for the class, do not infer from a sibling class that has it.

## 6.2 Sidecar Server (Python)

### 6.2.1 Responsibility

Accept the mod's connection (authenticated), manage the LLM conversation and tool loop, translate tool calls into protocol requests, persist sessions, and log actions.

### 6.2.2 Technical Stack

| Concern | Choice (D7) |
| --- | --- |
| WebSocket server | `websockets` |
| LLM SDKs | `anthropic`, `openai` (OpenAI-compatible endpoints via base-URL config) behind one adapter interface |
| Config | `pydantic-settings` (env vars, `.env`, config file) |
| Session store | SQLite (single file under `session_dir`) |
| Packaging | Single project run with `uv run`; a `launch` script/batch file ships with the mod |

### 6.2.3 LLM Integration

**Tool calling (D5):** native function-calling API of the provider. Text/JSON-mode parsing is not used. The adapter interface normalises tool calls and results across providers.

**Tool set exposed to the LLM** (compact; each maps to a protocol request):

| Tool | Maps to | Purpose |
| --- | --- | --- |
| `search_object_catalog(query)` | sidecar-local | Name/keyword → object ID(s), with category. Catalog is a static data file shipped with the sidecar. The LLM must not invent IDs. |
| `get_editor_state()` | `get_editor_state` | Camera, layer, ground Y, grid size, selection summary. |
| `summarize_region(x_range?, y_range?, editor_layer?)` | `summarize_region` | Counts by object type, bounds. Preferred over bulk queries. |
| `query_objects(filters, limit?, cursor?)` | `query_objects` | Paged, capped listing. |
| `get_object_properties(object_id)` | `get_object_properties` | Single-object detail. |
| `create_objects(objects[])` | `create_objects` | Batch creation. |
| `modify_objects(targets[], set?, delta?)` | `modify_objects` | Property set and relative deltas. |
| `delete_objects(targets[])` | `delete_objects` | Deletion (subject to per-turn cap). |
| `set_selection(targets[])` | `set_selection` | Highlight/select. |

Transactions are managed by the sidecar automatically (one per turn); they are not LLM tools.

**Loop limits (configurable, §10.2):** `max_tool_rounds` (default 10), `max_objects_per_turn` (default 5000, mirrors the mod limit), per-turn token budget, wall-clock timeout per turn. On hitting a limit the turn ends with `status: "incomplete"` and the assistant states what was and wasn't done.

**System prompt must contain:**

- Role and constraints (edits only; no saving; no trigger-specific properties in v1).
- Coordinate system: x increases right, y increases up, units are GD units, **30 units = one grid block**, `ground_y` comes from `get_editor_state`.
- The property schema (§7.5) and that unknown properties are rejected.
- Rules: look up object IDs via `search_object_catalog`; prefer `summarize_region` before `query_objects`; use batch creation; prefer relative `delta` for moves; never exceed limits; after a revert notice, don't assume prior changes exist.
- Domain facts needed for sensible layouts (block spacing, gamemode/speed spacing tables) — to be authored as a separate reference file included in the prompt or exposed as a lookup tool.

**Conversation history:** keep the last N turns within `history_token_budget`; older turns are truncated in v1 (summarisation is a v1.1 item). Tool results from prior turns are dropped from history after the turn ends, keeping only the assistant's final message and action summary.

**Streaming:** optional; protocol support defined in §7.4 (`assistant_message_delta`), not required for v1.

**Error handling:** exponential backoff with jitter on provider 429/5xx (bounded retries); provider errors surface to the panel via `sidecar_status` or `system_message` with a human-readable message; API keys are redacted from all logs and error strings.

**Sampling defaults:** low temperature (0.2) for deterministic edits.

### 6.2.4 State Management (D4)

The sidecar keeps no mirror of level contents. Each turn it receives a compact `get_editor_state`/`summarize_region` snapshot on demand. Reasons: user edits, undo, and reverts all invalidate a mirror, and the cost of resync exceeds the cost of re-querying. The sidecar does persist: conversation history, the action log, and a record of recent transactions (`txn_id`, summary, reverted flag).

### 6.2.5 Session Keying

One session per level, keyed by `level_key` supplied in `hello`. Default `level_key` = level name (collision and rename behaviour is an open question, §15). Switching levels (new editor session, new connection) selects or creates the session for that level; panel UI state resets.

# 7. WebSocket Protocol Specification

## 7.1 Transport

| Property | Value |
| --- | --- |
| Protocol | WebSocket (RFC 6455) |
| Default URL | `ws://127.0.0.1:8765` (sidecar listens; mod connects) |
| Encoding | UTF-8 JSON text frames |
| Max frame | 1 MB default (see §6.1.4) |
| Connections | Exactly one authenticated client at a time; additional connections are closed with code 4003 |
| Protocol version | Integer, currently `1`, carried in `hello` |

## 7.2 Authentication and Handshake

Threat addressed: web pages and other local processes connecting to a loopback socket (WebSockets are not subject to same-origin policy).

- On first start the sidecar generates a random token (≥ 128 bits) and writes it to a file under the user's local application-data directory (e.g., `%LOCALAPPDATA%\gd-ai-assistant\token`). The mod reads this file on each connection attempt. The token is **not** the LLM API key.
- The sidecar rejects any connection whose HTTP upgrade request carries an `Origin` header (browsers always send one; the mod does not).
- The first message from the mod must be `hello` carrying the token. Anything else, or a wrong token, closes the connection with code 4001 (`UNAUTHORIZED`). The token is sent in the message, not an HTTP header, because header support in the client library is unconfirmed.
- Accepted residual risk: other processes running as the same OS user can read the token file. This matches the trust level of the API key file itself.

### `hello` (mod → sidecar, request)

```json
{
  "type": "hello",
  "id": "m-1",
  "payload": {
    "protocol_version": 1,
    "token": "<token>",
    "client": { "mod_version": "0.1.0", "gd_version": "2.2081", "geode_version": "5.0.0" },
    "level": { "key": "My Level", "name": "My Level", "object_count": 1284 }
  }
}
```

### `hello` response (sidecar → mod)

```json
{
  "type": "response",
  "id": "m-1",
  "payload": {
    "ok": true,
    "result": {
      "protocol_version": 1,
      "sidecar_version": "0.1.0",
      "state": "ready",
      "detail": null,
      "history_count": 12
    }
  }
}
```

`state` is `"ready"` or `"needs_setup"` (e.g., no API key; `detail` carries a user-facing message). A protocol-version mismatch returns `ok: false` with `INVALID_REQUEST` and closes with code 4002.

After a successful `hello`, the sidecar pushes a `history` event (last N messages) so the panel can render prior conversation for this level.

## 7.3 Message Envelope

Every message uses the same envelope:

```json
{
  "type": "string",
  "id": "string or null",
  "payload": {}
}
```

| Field | Description |
| --- | --- |
| `type` | Message kind. Requests use the command name; replies use `"response"`; events use the event name. |
| `id` | Correlation ID for requests and their responses. `null` for events. Either side may issue requests; IDs are unique per sender. |
| `payload` | Message-specific data. |

**Responses** (`type: "response"`, `id` = request ID):

```json
{ "type": "response", "id": "req-42", "payload": { "ok": true, "result": { } } }
```

```json
{
  "type": "response",
  "id": "req-42",
  "payload": {
    "ok": false,
    "error": { "code": "OBJECT_NOT_FOUND", "message": "No matching objects.", "details": { } }
  }
}
```

Unsolicited errors use an `error` event (§7.6). Each request kind has a documented timeout; a missing response after the timeout is treated as `INTERNAL_ERROR` by the caller.

**Invariant: a response never draws a reply.** A frame of type `response` answers an earlier request and is never itself a request, so it must not be answered — *including* when it is malformed. Answering a malformed response with an error response lets the two sides echo each other without bound: the invalid reply is rejected, the rejection is replied to, and so on. This is not hypothetical; it happened during Phase 1, when one side sent `"id": {}` (see §6.1.11's `matjson` note) and the resulting loop flooded the log at roughly 200 warnings per second until the connection was killed.

Both sides therefore resolve or discard responses *before* any validation that could generate a reply. The sidecar checks `type == "response"` ahead of envelope validation, and the mod ignores any frame that is a `response` or carries a `null` id (the shape of an event, §7.4). A regression test covers the sidecar half.

## 7.4 Chat Messages

### Mod → Sidecar (events)

| type | Payload | Notes |
| --- | --- | --- |
| `user_message` | `{ message_id, text }` | `text` length-limited by `max-message-length`. |
| `cancel_turn` | `{ turn_id }` | Sidecar aborts the provider call, ends the open transaction, and finishes with `status: "cancelled"`. |
| `transaction_reverted` | `{ txn_id }` | Sent after the user reverts via the UI; sidecar updates its log and informs the LLM next turn. |

### Sidecar → Mod (events)

| type | Payload | Notes |
| --- | --- | --- |
| `turn_started` | `{ turn_id, message_id }` | Mod shows the thinking indicator. |
| `assistant_message` | `{ turn_id, message_id, text }` | Final text for a message. May occur more than once per turn. |
| `assistant_message_delta` | `{ turn_id, message_id, delta }` | Optional (streaming). Mod keeps a `message_id` → node map. |
| `turn_finished` | `{ turn_id, status, actions, txn_id?, usage? }` | `status`: `completed`, `cancelled`, `failed`, `incomplete`. `actions`: array of `{ kind, count, summary }`. |
| `sidecar_status` | `{ state, detail }` | `state`: `ready`, `needs_setup`, `provider_error`. |
| `system_message` | `{ level, text }` | `level`: `info`, `warn`, `error`. |
| `history` | `{ messages: [ { role, text, ts } ] }` | Sent once after `hello`. |

"N actions" in the summary bubble counts **mutating tool calls**, not objects; per-action `count` is objects affected.

## 7.5 Editor Requests (Sidecar → Mod)

All return the uniform response envelope; `result` shapes are shown.

| type | Payload | `result` | Notes |
| --- | --- | --- | --- |
| `get_editor_state` | `{}` | `{ object_count, selected: { count, by_type, bounds }, editor_layer, camera: { x, y, zoom, visible_rect }, ground_y, grid_size, playtesting, level: { name } }` | `grid_size` is 30. |
| `query_objects` | `{ filters, limit?, cursor? }` | `{ objects, total_matches, next_cursor?, truncated }` | See filter and object schemas below. |
| `summarize_region` | `{ x_range?, y_range?, editor_layer? }` | `{ total, bounds, by_type: [ { id, count } ] }` | `by_type` capped at 20 entries. |
| `get_object_properties` | `{ object_id }` | `{ properties }` | Full property set for one object. |
| `create_objects` | `{ objects: [ { id, x, y, properties? } ] }` | `{ created: [ object_id ], count }` | Validated whole before any mutation. |
| `modify_objects` | `{ targets: [ object_id ], set?, delta? }` | `{ modified_count, missing: [ object_id ] }` | `delta` supports `x`, `y`, `rotation` only. Missing IDs are reported, not fatal unless none were found. |
| `delete_objects` | `{ targets: [ object_id ] }` | `{ deleted_count, missing: [ object_id ] }` | Same missing-ID semantics. |
| `set_selection` | `{ targets: [ object_id ] }` | `{ selected_count }` | |
| `begin_transaction` | `{ label }` | `{ txn_id }` | §6.1.5. |
| `end_transaction` | `{ txn_id }` | `{ txn_id, summary: { created, modified, deleted } }` | |
| `revert_transaction` | `{ txn_id }` | `{ reverted: true }` | Usually triggered from the UI, not the sidecar. |
| `ping` | `{}` | `{ pong: true }` | Health check. |

**`query_objects` filters:** `id` (object type ID), `ids` (unique IDs), `editor_layer`, `z_layer`, `group`, `x_range` `[min, max]`, `y_range` `[min, max]`, `selected_only`. Defaults: `limit` 100, maximum 500; results beyond the limit set `truncated: true` and a `next_cursor`.

**Object record** (compact; only non-default properties are included):

```json
{ "object_id": 4821, "id": 8, "x": 300.0, "y": 105.0, "rotation": 0 }
```

**Property schema (v1):**

| Key | Type | Notes |
| --- | --- | --- |
| `x`, `y` | number | GD units. 30 units = one grid block. |
| `rotation` | number | Degrees. |
| `scale_x`, `scale_y` | number | Positive multiples. |
| `flip_x`, `flip_y` | boolean | |
| `z_layer` | enum | Named layer (e.g., `b4`, `b3`, `b2`, `b1`, `default`, `t1`, `t2`, `t3`; verify names against the game's z-layer list). |
| `z_order` | integer | |
| `editor_layer` | integer | Primary editor layer; interpretation (0- vs 1-based) to be fixed in S-bindings (verify). |
| `groups` | integer[] | Group IDs. |
| `color_channel_1`, `color_channel_2` | integer | Colour channel IDs for base/detail. |

Unknown keys are rejected with `UNSUPPORTED_PROPERTY`; values outside range are rejected with `INVALID_REQUEST`. Trigger-specific properties are out of scope for v1.

**Object identity (D9):** `object_id` is the object's integer unique ID, a JSON number, valid for the current editor session. The mod resolves it with `findGameObject` on the main thread for each command. Do not persist `object_id` across sessions.

**Object type IDs:** the mod checks that `id` is within a sane range and treats a null return from `createObject` as `INVALID_OBJECT_ID`. A denylist of IDs known to crash on programmatic creation is maintained by the mod (populated during testing, S-bindings). The sidecar catalog is the primary guard.

## 7.6 Events (Mod → Sidecar)

| type | Payload | Notes |
| --- | --- | --- |
| `editor_closed` | `{}` | Sent when the editor closes; the mod then closes the socket (code 1000). |
| `level_changed` | `{ added, removed, modified, bounds?, source }` | Coalesced (§6.1.7). `source`: `user` or `assistant`. |
| `selection_changed` | `{ count, ids, truncated }` | Throttled; `ids` capped at 200. |
| `progress` | `{ request_id, done, total }` | For long jobs only. |
| `error` | `{ code, message }` | Unsolicited errors (not a response to a request). |

`editor_ready` from 0.3.0 is removed; `hello` carries the same information.

## 7.7 Error Codes

| Code | Meaning |
| --- | --- |
| `INVALID_REQUEST` | Malformed message, wrong types, out-of-range values, unsupported protocol version. |
| `UNAUTHORIZED` | Missing/incorrect token (connection-level; close code 4001). |
| `UNSUPPORTED_PROPERTY` | Unknown property key. |
| `INVALID_OBJECT_ID` | Object type ID not valid or not creatable. |
| `OBJECT_NOT_FOUND` | None of the referenced unique IDs exist. |
| `LIMIT_EXCEEDED` | A count or queue limit was exceeded (`details.limit`). |
| `PAYLOAD_TOO_LARGE` | Message exceeded the frame limit. |
| `EDITOR_NOT_OPEN` | No editor layer is active. |
| `EDITOR_BUSY` | Editor in a state that disallows edits (e.g., playtest). |
| `TRANSACTION_ACTIVE` | A transaction is already open. |
| `TRANSACTION_NOT_REVERTIBLE` | Transaction is not the most recent change. |
| `CANCELLED` | Request cancelled by the sender or by disconnect. |
| `THREAD_DISPATCH_FAILED` | Could not schedule work on the main thread. |
| `INTERNAL_ERROR` | Unexpected failure, including request timeouts on the caller side. |

## 7.8 Connection Lifecycle

1. Sidecar starts, generates/loads the token, and listens on `127.0.0.1:8765`. If the port is busy it exits with a clear message (the port is configurable).
2. User enters the editor. If `auto-connect` is enabled, the mod reads the token file and connects; otherwise the user presses **Connect** in the panel.
3. Mod sends `hello`; sidecar validates token and version and responds; sidecar pushes `history`.
4. Normal request/event flow proceeds.
5. On disconnect, the mod shows "Disconnected", ends any open transaction, and (if `auto-connect`) retries with exponential backoff (1 s doubling to a 30 s cap).
6. On editor close, the mod sends `editor_closed`, ends any open transaction, and closes the socket.
7. The sidecar treats a lost connection mid-turn as `status: "incomplete"` and keeps the transcript.

# 8. UI Specification

## 8.1 Chat Panel

| Property | Value |
| --- | --- |
| Base | Custom non-modal panel node attached to `EditorUI` as a plain `CCLayer` (D8; S6 resolved in 0.9.0) |
| Default size | 216 × 196 **points** (GD's coordinate space is 320 points tall); never taller than the visible area minus margins |
| Position | Bottom-right with margin |
| Resizable / draggable | No (v1) |
| States | Expanded, collapsed (title-bar chip) |
| Toggle | Geode keybind `toggle-chat`, default Ctrl+Shift+C |
| Lifetime | Created with the editor layer, destroyed when it closes |
| Touch | Claims the **message area only**, at priority `-1`. Lower values dispatch first, so a lower value steals scroll drags from the panel's own `ScrollLayer`; `-1` sits behind the children and ahead of the editor layer at `0`. Claiming the whole panel makes Send dead. |

## 8.2 Layout

```text
┌─────────────────────────────────────────────┐
│ ● LLM Assistant                      [–]    │  ← header: status dot, collapse
├─────────────────────────────────────────────┤
│                                             │
│   [Message scroll area]                     │
│                                             │
│   You: Add a row of spikes                  │
│                                             │
│   Assistant: I'll add 5 spikes...           │
│                                             │
│   [icon] Finished with 2 actions   [Revert] │
│                                             │
├─────────────────────────────────────────────┤
│ [ Type your message...        ] [Send/Stop] │
└─────────────────────────────────────────────┘
```

## 8.3 Message Display Area

| Property | Value |
| --- | --- |
| Component | Scroll layer containing a stack of message nodes |
| Text renderer | `geode::Label` with `chatFont.fnt`. **Not** `bigFont.fnt`: it is size 32 and draws lowercase as capitals, so body text becomes an all-caps wall at double height. `chatFont.fnt` is size 16 with real lowercase. Markdown is **not** rendered (`MDTextArea` unused); fence lines are stripped so they are not shown literally. |
| User messages | Right-aligned, distinct background |
| Assistant messages | Left-aligned, default background |
| System messages | Centered, muted, smaller font |
| Auto-scroll | To bottom on new message unless the user has scrolled up |
| History cap | Render the most recent 100 messages; older ones collapse into a "Show earlier" control |

### 8.3.1 Sanitising Untrusted Text

Assistant text (and anything echoed from level data) is untrusted. Before it reaches `MDTextArea` the mod:

- Strips links, images, raw HTML, profile references, and GD colour tags (`<cr>`, `<cg>`, `<c-XXXXXX>`) — the last of these matters because `geode::Label::setRichText` interprets colour tags, so unstripped tags would be *rendered*, not shown as text.
- Allows bold, italic, inline code, and fenced code blocks.
- Renders through `geode::Label`, which is quad-based and supports Unicode and emoji where the chosen font supplies the glyphs. The bitmap-font glyph-coverage spike (S5) is **retired**. Remaining constraint: pick one font covering Latin-1 plus common symbols, and decide the fallback for glyphs a font lacks (a missing glyph now degrades rather than breaking layout).

Mod-generated system messages may use colour tags.

## 8.4 Message Types

| Type | Visual | Behaviour |
| --- | --- | --- |
| User message | Right-aligned bubble | Static text |
| Assistant message | Left-aligned bubble, Markdown | Updated in place if streaming |
| Thinking indicator | Animated ellipsis in an assistant bubble | Replaced by the response |
| Action summary | Left-aligned compact bubble with sprite icon | Tap to expand; includes **Revert** while revertible |
| Error message | Left-aligned, red-tinted bubble | Human-readable text; code in smaller print |
| System notification | Centered, muted | "Connected", "Disconnected", "Reconnecting…" |

## 8.5 Input

| Property | Value |
| --- | --- |
| Component | `geode::TextInput`, with `setCommonFilter(CommonFilter::Any)`. The default filter rejects punctuation including `!` and `'`. |
| Placeholder | "Ask about this level..." |
| Send | Enter or Send button |
| Max length | `max-message-length` (default 2000) |
| While processing | Input disabled and greyed; the Send button becomes **Stop** (sends `cancel_turn`) |
| Multi-line | No (v1) |
| Focus behaviour | Enter is caught in a `$modify` hook on `EditorUI::keyDown` (S6). While a GD text field has focus, keyboard events do **not** reach a mod layer at all, and `TextInput` does not forward `enterPressed`; see §0.1. |

## 8.6 Keyboard

| Shortcut | Action |
| --- | --- |
| Ctrl+Shift+C (rebindable) | Toggle panel expanded/collapsed |
| Enter | Send message, when the input has focus. Routed via `EditorUI::keyDown`; see §0.1. |
| Escape | **Not implemented.** Input focused: unfocus. Otherwise: collapse the panel. |

## 8.7 Empty State

When no messages exist, display a centred placeholder:

"Ask me to modify your level. Try: 'Add a row of spikes along the ground' or 'Remove all objects in the background layer'."

## 8.8 Connection and Setup States

| State | Panel shows |
| --- | --- |
| Sidecar not reachable | Header dot red; message: "Can't reach the sidecar at <url>. Start it with the launcher script, then press Connect." |
| Connected, `needs_setup` | Header dot amber; the sidecar's `detail` (e.g., "No API key configured. Set it in the sidecar config and restart."); input disabled |
| Connected, ready | Header dot green |
| Reconnecting | Header dot amber; "Reconnecting…" |
| Auth failure (4001) | Red; "Token mismatch. Restart the sidecar to regenerate it." |

## 8.9 Thinking and Action Logging

**Thinking:** a label cycling `.`, `..`, `...` every 0.4 s in an assistant bubble (replace the 0.3.0 mock's `●●●`).

**Action summary:** after `turn_finished`, replace the indicator with a compact bubble:

```text
Finished with 3 actions
```

Tap to expand:

```text
Finished with 3 actions
  - Created 5 objects (spikes) at y=120
  - Modified 2 objects (colour channel 1 -> 2)
  - Deleted 1 object (id: 98765)
```

Prefer plain ASCII and sprite icons for alignment and legibility. `geode::Label` renders Unicode and emoji where the chosen font supplies the glyphs, so a missing glyph degrades gracefully rather than breaking layout; the 0.3.0 mock's `●●●` is still replaced by the ASCII ellipsis cycle above.

## 8.10 Message History

- In-memory for the editor session. After popup collapse/expand the scroll position resets to the bottom.
- Full history is persisted by the sidecar; after `hello` the sidecar pushes the last N messages.

# 9. Configuration System

## 9.1 Mod Settings

See §6.1.8.

## 9.2 Sidecar Configuration

Loaded from environment variables, a `.env` file, or a config file.

| Setting | Description | Default |
| --- | --- | --- |
| `llm_provider` | Provider adapter (`anthropic`, `openai`, `openai_compatible`) | none (required) |
| `api_key` | API key for the provider. Never logged, never sent to the mod. | none (required) |
| `model` | Model identifier. **No default**: forces an explicit, current choice. | none (required) |
| `base_url` | Optional endpoint override (OpenAI-compatible providers) | provider default |
| `max_tokens` | Maximum tokens per LLM response | 4096 |
| `temperature` | Sampling temperature | 0.2 |
| `ws_host` | Bind address. Non-loopback values are refused unless `allow_non_loopback` is set. | 127.0.0.1 |
| `ws_port` | WebSocket listen port | 8765 |
| `token_path` | Location of the handshake token file | `%LOCALAPPDATA%\gd-ai-assistant\token` |
| `session_dir` | Directory for session persistence | `./sessions` |
| `max_tool_rounds` | Tool-loop rounds per turn | 10 |
| `max_objects_per_turn` | Objects the sidecar will request mutating per turn | 5000 |
| `turn_token_budget` | Total LLM tokens per turn | provider-dependent, required to be set |
| `turn_timeout_s` | Wall-clock cap per turn | 120 |
| `history_token_budget` | Tokens of history sent per request | 8000 |
| `log_level` | Logging verbosity | `info` |

With no `api_key`/`model` configured the sidecar starts, accepts `hello`, and reports `state: "needs_setup"`.

## 9.3 Where Each Setting Lives

Mod: connection URL, auto-connect, message length, frame budget, per-request object cap. Sidecar: everything about the LLM and the tool loop. The mod's per-request caps are authoritative (D10); sidecar values must be ≤ the mod's.

# 10. Open Questions Resolved by This Revision

See the decision log (§5.3). Remaining open questions are in §15.

# 11. Error Handling Summary

- Mod errors are returned in the response envelope (§7.3) with codes from §7.7.
- Sidecar/provider errors reach the user via `sidecar_status` or `system_message`, not as editor error codes.
- User-visible text is human-readable; codes appear only in small print.
- All unexpected exceptions on either side are logged with a correlation ID matching the request `id`.

# 12. Security Considerations

| Threat | Mitigation |
| --- | --- |
| API key exposure via game memory | Key exists only in the sidecar process. |
| API key exposure via mod settings | Not a mod setting; stored in sidecar config. Recommend environment variable or file with restricted permissions; OS keyring support is a v1.1 item. |
| API key exposure via logs/errors | Redaction in all logs and user-facing errors. |
| Web page or other process connecting to the socket | Token handshake in `hello`; sidecar rejects connections carrying `Origin`; single-client limit; loopback binding enforced by default (D11). |
| Same-user local process reading the token | Accepted residual risk (same trust level as the API key file). |
| Command injection via editor API | The mod validates every field (types, ranges, IDs, limits) before any mutation; unknown commands and properties are rejected. |
| Denial of service via rapid or huge commands | Limits and queue depth enforced **in the mod** (D10); time-sliced execution; request timeouts. |
| Crash from off-thread API calls | All editor access on the main thread via the job queue; no pointers held across frames (§6.1.3). |
| Prompt injection via level content (text objects, level name/description) | Treat all level-derived text as untrusted in the system prompt; bounded blast radius: only editor operations exist as tools, per-turn caps, one undo group per turn, `Revert` action. |
| Untrusted LLM output in the UI (links, colour tags, profile refs) | Sanitisation per §8.3.1. |
| Level data sent to a third party | Document clearly in the README and first-run panel that level contents are sent to the configured LLM provider; nothing is sent until the user submits a message. |
| Supply chain (sidecar and mod dependencies) | Pin versions with a lockfile; avoid unmaintained libraries where an alternative exists. |
| Runaway cost | `max_tool_rounds`, per-turn token budget, turn timeout; usage reported in `turn_finished`. |

# 13. Testing Strategy

A Geode mod cannot run headless in CI, so the protocol is the test seam.

- **Protocol as source of truth:** JSON Schemas for every message live in a `protocol/` directory; both sides validate against them in tests.
- **Fake mod (Python):** speaks the protocol to exercise the sidecar: handshake, auth failures, `Origin` rejection, limits, disconnect/cancel behaviour.
- **Fake sidecar:** a script that drives the mod through every request, including malformed and oversized ones, for manual and semi-automated in-editor runs.
- **Scripted LLM provider:** deterministic tool-call transcripts to test the loop, caps, cancellation, and error paths without network or cost.
- **Record/replay:** capture real turns for regression tests.
- **In-editor test matrix:** 1000- and 5000-object batches (frame-time measurement), undo/revert grouping, playtest mode, user edits during a turn, editor close mid-turn, sidecar kill mid-turn, non-ASCII input, very long messages, 100-turn soak test with random user edits (A4).
- **Fuzzing:** random and malformed payloads against every request type; the mod must reject without crashing.

# 14. Implementation Roadmap

## Phase 0 — Spikes (exit before Phase 1)

| ID | Question | Exit criterion |
| --- | --- | --- |
| S1 | Does the verified toolchain build and load a hello-world mod on Geode SDK 5.11.1 / loader 5.9.0? (The "is v5 stable" half is settled: v5 is the target, v6 is WIP.) | Mod builds and loads on the target GD/Geode versions. The toolchain is newer than Geode's tested matrix, so clear this before any other work. |
| S2 | **RESOLVED (0.6.0).** No WebSocket dependency: hand-written RFC 6455 client on `arc::TcpStream` (D14, §6.1.10), plus a bundled SHA-1 for the handshake. | **Met.** Round trip verified against the Python sidecar: handshake accepted, token accepted, `hello` parsed, response and `history` frames decoded, clean close. Token handshake and the absence of an `Origin` header confirmed on the wire. |
| S3 | **Advanced:** can undo entries be grouped per transaction via `addToUndoList` / `handleAction` (§6.1.5)? | Either grouping works (A2 via Ctrl+Z) or the bookkeeping fallback is adopted. Also measure per-slice refresh cost and `m_increaseMaxUndoRedo` behaviour at 5000 objects. |
| S4 | Which hooks reliably detect user adds/removes/moves/selection? | Coalesced `level_changed` working with correct `source` tagging. |
| S6 | **RESOLVED (0.9.0).** Non-modal panel attachment, touch/key routing, hotkey leakage, Escape handling. | **Met, with corrections.** The panel attaches to `EditorUI` as a plain `CCLayer` and the editor stays fully usable. Two findings contradict the original assumptions: Touch priority must be **behind** the panel's own children (`-1`), not ahead of them, or the panel steals scroll drags from its own `ScrollLayer`; and keyboard input does **not** reach a mod layer at all while a text field has focus — `EditorUI::keyDown` is the only route that works. Escape handling is still unverified. |
| S7 | Unique ID stability across undo/redo; which object IDs crash on programmatic creation. | D9 confirmed or revised; initial denylist. |

S5 (bitmap-font glyph coverage) was retired in 0.5.0 — see §8.3.1. All other spike IDs are unchanged, so existing references to S4, S6 and S7 remain valid.

## Phase 1 — WebSocket Bridge

**Status: exit criteria met (0.7.0).** Verified in the editor: connection on editor open, `hello` handshake, `ping` and `get_editor_state` round trips, and a clean close on editor close. See §6.1.11 for the lifecycle mechanics and §7.3 for the response invariant.

- Client connection, `hello`/token/Origin handling, `ping`, `get_editor_state`.
- Reconnect with backoff (1 s doubling to a 30 s cap).
- **Exit:** a Python server and a fake mod complete the handshake; editor state queries work without frame hitches; A5.

Not yet exercised: reconnection after a mid-session sidecar restart, and the 4001 token-mismatch retry path. Both need deliberate fault injection and are carried into Phase 2.

## Phase 2 — Editor API Layer

- `create_objects`, `modify_objects`, `delete_objects`, `set_selection`, `query_objects`, `summarize_region`, `get_object_properties`.
- Job queue with time slicing; limits; validation before mutation.
- Transactions and revert; coalesced events.
- **Exit:** A1, A2 (or fallback), A3 (mod side), fuzz tests pass.

## Phase 3 — Chat UI

**Status: exit criteria met (0.9.0).** The panel works end to end against the scripted
sidecar: connection state shown, messages sent and rendered, thinking indicator,
turn summary, cancellation path available, and the transcript scrollable. Verified in
the editor; see §0.1 for the measurements and the traps found.

- Panel shell, message list, input with Send/Stop, sanitiser, thinking indicator, action summary with Revert, setup/connection states.
- **Exit:** panel works end-to-end against the fake sidecar.

Not yet done, and deliberately so: Markdown rendering (`MDTextArea`) and the `Revert`
action on the summary bubble. Revert needs a transaction id from the sidecar, which the
scripted provider does not open. The mouse wheel does not scroll the transcript; drag
does.

## Phase 4 — Sidecar

**Status: implemented and tested headlessly (0.10.0). In-editor verification is blocked
by §0.1.**

- WS server and auth, provider adapters, tool loop with caps, catalog, session persistence, cancellation.
- **Exit:** scripted-provider tests pass; A5, A6.

Delivered: a provider interface with Anthropic, OpenAI and scripted adapters; the tool
loop with lazy transactions, per-turn object caps, round and token caps, a wall-clock
timeout, bounded retry with jitter, and cancellation that closes its transaction; the
tool schemas mapped to protocol requests; the object catalog and search tool; the system
prompt. **102 tests pass**, and every frame either side sends is validated against
`protocol/v1` rather than eyeballed.

Known gaps: session persistence to SQLite (SPEC.md 6.2.2) is not implemented, so history
lives in memory and is lost when the sidecar restarts. The catalog is a two-entry seed
by design -- an unverified id would make the model place the wrong object silently -- and
needs a real source before it is useful (SPEC.md 15). Streaming (`assistant_message_delta`)
is unused: the protocol supports it, the adapters do not emit it, and the mod renders
whole messages.

## Phase 5 — Integration and Polish

**Blocked on §0.1** for everything that requires the editor to run. The parts that do not
-- documentation, packaging, the launcher script -- can proceed.

- End-to-end user flow, error cases, soak test (A4), documentation, release packaging, README data-disclosure text, launcher script.
- Confirm Geode index policy compliance for network-using / LLM-integrated mods before release.

# 15. Remaining Open Questions

| # | Question | Impact |
| --- | --- | --- |
| 1 | Session key: level name collides across levels and breaks on rename. Is a mod-stored name→UUID map worth it? | Session continuity. |
| 2 | How much trigger support belongs in v1.1, and what is the property schema (target group, move offsets, duration, easing)? | Largest driver of usefulness. |
| 3 | **Partly resolved in 0.5.0.** Geode v5 is the target (SDK 5.11.1 verified locally, loader 5.9.0 installed); v6 exists only as a WIP migration guide, so no v6 work is in scope. Still open: what share of players are on v5 versus v4, and is a v4 build required for reach? | Reach only — the async API question is settled. |
| 4 | Does the Geode index permit or require disclosure for mods that connect to local services / send data to LLM providers? | Release eligibility. |
| 5 | Which provider and model are the supported baseline for tool-use quality testing? | Prompt tuning, test matrix. |
| 6 | Should confirmation be required before large destructive operations (e.g., deleting more than N objects), or is revert sufficient? | Safety vs friction. |
| 7 | Streaming in v1 or later? | UI complexity; protocol already supports it. |
| 8 | Domain-knowledge reference (spacing tables, gamemode/speed data): static prompt text, lookup tool, or both? | Quality of generated layouts. |

# 16. References

| Resource | URL |
| --- | --- |
| Geode SDK Documentation | https://docs.geode-sdk.org |
| Geode v5 Async guide | https://docs.geode-sdk.org/tutorials/async |
| Geode waitForMainThread | https://docs.geode-sdk.org/functions/geode/async/waitForMainThread/ |
| Geode v5 Migration Guide | https://docs.geode-sdk.org/tutorials/migrate-v5 |
| Geode v5 → v6 migration guide (v6 WIP) | https://docs.geode-sdk.org/tutorials/migrate-v6 |
| Geode required C++ tools | https://docs.geode-sdk.org/getting-started/cpp-stuff |
| Geode CLI | https://docs.geode-sdk.org/getting-started/geode-cli |
| Geode web requests (HTTP only — no WebSocket) | https://docs.geode-sdk.org/tutorials/fetch |
| Geode v5.0.0-alpha.2 release notes | https://newreleases.io/project/github/geode-sdk/geode/release/v5.0.0-alpha.2 |
| Geode bindings: LevelEditorLayer | https://docs.geode-sdk.org/classes/LevelEditorLayer |
| WSLiveEditor (reference mod) | https://geode-sdk.org/mods/iandyhd3.wsliveeditor |
| WSLiveEditor GitHub | https://github.com/iAndyHD3/WSLiveEditor |
| Geode UI Components (DeepWiki) | https://deepwiki.com/geode-sdk/geode/4.4-ui-nodes-and-components |
| Geode mod.json Configuration | https://docs.geode-sdk.org/mods/configuring/ |
| Geode Editor Object Creation (Issue #1064) | https://github.com/geode-sdk/geode/issues/1064 |

# Appendix A — Verification Checklist

Items asserted in this document that are not yet confirmed, with their 0.5.0 status:

1. **Resolved (0.5.0):** Geode v5 is the target — SDK 5.11.1 verified locally, loader 5.9.0 installed; v6 is WIP. Only the player-share / v4-build question remains (§15 Q3).
2. `geode::Popup` modality/overlay behaviour (§6.1.2); panel attachment point and input routing (S6).
3. **Resolved (0.6.0):** Geode provides no WebSocket client (`web::WebRequest` is HTTP-only), and Geode/Arc provide no SHA-1. Both are supplied in-mod: a hand-written RFC 6455 client and SHA-1 (§6.1.10, D14), verified by a round trip against the Python sidecar.
4. **Advanced (0.5.0):** grouping is feasible via `addToUndoList` / `handleAction` / `m_undoObjects`; confirm whether a single `UndoObject` may mix kinds (S3).
5. Hooks for change detection (S4).
6. **Retired (0.5.0):** `geode::Label` is Unicode- and emoji-capable, so only font choice and the missing-glyph fallback remain (§8.3.1).
7. Unique-ID stability across undo/redo; crash-prone object IDs (S7).
8. `createObjectsFromString` object limit value and suitability as a batch fast path.
9. **Resolved (0.5.0):** Arc provides `arc::oneshot`, `arc::mpsc`, `arc::Semaphore` and `arc::Notify`; `arc::Mutex` is the async-aware lock.
10. **Resolved (0.5.0):** no generic editor `update()` exists; vanilla `updateObjectLabel` / `updateObjectSection` / `quickUpdateAllPositions` / `updateGridLayer` / `updateGameObjects` replace it, so `nwo5` is not needed (§6.1.6).
11. **Partly resolved (0.7.0):** `editor_layer` reported 0 for the base layer of a real level, which is consistent with 0-based indexing. Still unverified: z-layer enum names, and whether non-base layers report 1, 2, ... Full resolution belongs with S-bindings in Phase 2.
12. **Resolved (0.5.0):** keybinds are core Geode (`geode::Keybind`). Still unverified: whether Ctrl+Shift+C collides with editor shortcuts (S6).
13. Geode index policy for LLM/network mods.
14. **Resolved (0.5.0):** Arc ships Core and Time only; the rest must be enabled in `CMakeLists.txt` before the SDK is added (§6.1.9). Whether the newer-than-tested toolchain builds cleanly is S1.
