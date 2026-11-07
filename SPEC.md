# Geometry Dash LLM Assistant Mod — Full Specification

# 0. Document Overview

| Field | Value |
| --- | --- |
| Project name | GD AI Assistant |
| Document version | 0.11.0 |
| Status | Draft — Phases 0-4 implemented and **verified in-editor on GD 2.2091**. Phase 5 in progress. |
| Target platform | Windows only (initial release) |
| Target Geode version | v5.11.2 nightly (loader `57415d9`, the v6 merge). Requires C++23 |
| Target GD version | **2.2091** (was 2.2081; see 0.1) |

## 0.1 RESOLVED: GD 2.209 support (2026-10-31)

**The mod builds, loads and works in the editor on GD 2.2091.** The full stack was
verified in-game: panel, connection, a complete tool-loop turn, transaction grouping, and
Ctrl+Z reverting the whole turn.

What it took, because none of it was the obvious route:

1. **Wait for the v6 merge.** Geode merged `v6` into `main` as PR #2314 (`57415d9`,
   2026-10-30). That is the first commit with 2.209 support. There is still **no tagged
   release** — `main`'s `VERSION` file says 5.11.2, so the label is misleading and the
   commit is what matters.
2. **Install the matching nightly loader** (`geode-57415d9-win.zip`). The v5.11.2
   *release* installer is not v6 and does not support 2.2091.
3. **Point the SDK at `main`, not the tag.** `geode sdk update` follows releases and left
   the SDK on tag `v5.11.2`. A `git checkout main` in the SDK clone is what actually brings
   in the v6 headers. With that, the SDK's own default becomes `GEODE_GD_VERSION 2.2091`,
   so no override is needed in our `CMakeLists.txt` (an earlier attempt at forcing it is
   gone).
4. **Replace the SDK's import library.** `geode sdk install-binaries` fetches the release
   `Geode.lib` (~1,142 KB) which lacks v6 symbols; linking failed with undefined
   `EventCenterGlobal::addReceiver`, `matjson::Value(float)`, `matjson::ConstValueIterator`
   and others. Copying the nightly's `Geode.lib` (~1,161 KB) into `<SDK>/bin/5.11.2/` fixes
   it. The release lib is kept alongside as `Geode.lib.release-backup`.

**The dependency had to go.** `cvolton.level-id-api` does not compile against v6: its
`EditorIDs.hpp` is built on `GEODE_EVENT_EXPORT_*` macros and v6 reworked events. It is a
third-party mod needing its own v6 port. Removed from `mod.json`, with the single call site
(`EditorIDs::getID`) dropped; `Bridge::buildHello` already fell back to the level name,
which SPEC.md 6.2.5 lists as the default session key. **Cost: the session key is the level
name**, with the collision/rename caveat §15 already records.

**Destructive developer probes are now gated by an exact string token.** On 2026-10-31 the
probes ran on every editor open despite `debug-probe` defaulting to `false` and having no
saved value on disk — wiping the user's undo stack and adding objects to their level. The
`bool` setting was replaced by `probe-token`, which must equal `run-probes`; a string
comparison cannot misfire the way that read did, and it states the consequence in the
settings UI. The root cause of the bad read was not established, and is not worth chasing
now that the trigger cannot fire accidentally.

## 0.1a History: how GD 2.209 broke the toolchain (2026-08-16)

Kept because it records several wrong turns and the reasoning that finally unblocked it.
**Superseded by 0.1.**

Steam pushed Geometry Dash 2.209 on 2026-10-16 at 00:11. The game then crashed during
Geode's startup, before any mod loads, so nothing could be tested in the editor.

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

1. **Roll Geometry Dash back to 2.2081 — TRIED, NOT AVAILABLE.** The standard remedy in
   this scene, but Steam would not serve the older depot (build 25909607 is the current
   one). Recorded as attempted so it is not suggested again. Third-party downgrade tools
   exist and are not used here: they mean running an unverified binary against a game
   install.
2. **Wait for Geode.** The fix is not ours. Three loader builds have now been tried (see
   below) and none works, so this is the remaining option.
3. **Nightly and v5.11.2 loaders — TRIED, BOTH FAIL.** Neither restores in-editor testing.
   See the outcomes below. **v5.11.2 is installed**; the v5.11.1 release files are backed
   up at `%USERPROFILE%\geode-loader-backup-413ac9b`.
4. **Continue on the unblocked work** (see the table above).

### Loader attempts: three builds, three failures (measured 2026-10-16)

| Loader | Game runs? | Mods load? |
| --- | --- | --- |
| v5.11.1 release (`413ac9b`) | **No** — crashes at startup in `CCEGLView::onGLFWWindowFocus` via Geode's `GLFWFocusCallback` | n/a |
| nightly (`a6b9a91`) | Briefly, then crashes at the same place | **No** — version gate, then the export problem below |
| v5.11.2 release (`3909856`) | **Yes**, when launched **through Steam** | **No** — same export problem |

### The real blocker, identified precisely

**Geometry Dash 2.209 removed three exported functions from its own
`libcocos2d.dll`.** A mod built against the 2.2081 bindings imports them, so it cannot
load, and the only symptom is `ERROR_PROC_NOT_FOUND` — which names no symbol.

Found by diffing the import table of the built mod against the exports of the *new* game
DLL (`dumpbin /imports` and `/exports`). Our mod imports 1088 symbols from
`libcocos2d.dll`; exactly three are gone:

```
cocos2d::CCTouchDispatcher::registerForcePrio(cocos2d::CCObject*, int)
DS_Dictionary::loadRootSubDictFromCompressedFile(char const*)
DS_Dictionary::saveRootSubDictToCompressedFile(char const*)
```

This is the answer to "we don't know whether the API changed, and by how much". The API
*has* changed, in a small and specific way, and it is a **game-side** change: nothing about
it is Geode's doing or ours.

Two further checks that close off the alternatives:

- **Rebuilding will not fix it.** The `Geode.lib` import library shipped in v5.11.2 is
  **byte-identical** (SHA-256 `9EC4373A…`) to the SDK's 5.11.1 one, so a rebuild produces
  the same imports. The problem is the game's DLL, not Geode's.
- **`Geode.dll` is innocent.** All 94 of our imports from it are present in v5.11.2's 2418
  exports — zero missing. So the earlier `ERROR_PROC_NOT_FOUND` under the nightly was the
  same three cocos symbols, not a Geode ABI change; the ABI theory was wrong.

**When this clears:** a mod built against 2.209's cocos bindings, which requires the SDK
to publish them. That is the single remaining dependency, and it is upstream.

### Correction to an earlier reading

An earlier note in this section claimed v5.11.2 "removes the version gate" (0 errors
against 10). That was **wrong**, and worth recording because the mistake is easy to
repeat: the count was taken from a log that had *stopped early*. v5.11.2's run died at
`Entry took 0.017s`, before the version check had printed anything, so "0 errors" meant
"the check never ran". Under a successful launch v5.11.2 reports the same gate as every
other build. **A count of zero is only meaningful once you know the code that produces it
actually ran.**

### Launching the game: through Steam

Launches via `Start-Process GeometryDash.exe` died silently in ~2 s — no crash log, no
error line, no Windows application-error event. The same binary launched **through Steam**
runs. This is a Steam/DRM behaviour, not a Geode fault, and it invalidated several of the
earlier "the game dies" observations: those runs had simply not been started the way the
game expects. Any future in-editor testing must launch via Steam.

**v5.11.2 outcome.** Its notes say "Fix 2.209 crashes", and under a Steam launch the game
**does run**. Mods still do not load, and the reason is the cocos export removal described
above — not the loader.

An earlier version of this section claimed v5.11.2 "removes the version gate (zero errors
against ten)" and that the game died during startup. **Both were wrong.** The zero came from
a log that had stopped early, before the check ran, and the "death" was the non-Steam launch
path. Kept here because the two mistakes have the same shape: reading a measurement whose
preconditions had not been met.

**Nightly outcome.** Installed `geode-a6b9a91-win.zip` from the `nightly` pre-release. The
game launches but crashed about a minute in (same `onGLFWWindowFocus` stack), and **not one
mod loads**, for two independent reasons that each needed an experiment to find:

1. **The version gate.** Every mod declares `gd.win = 2.2081`, and the loader rejects each
   one with `This mod doesn't support this version of Geometry Dash (2.2081)` — 11 errors,
   one per installed mod. Repackaging our mod's manifest as `2.209` (bypassing CMake's
   check with `geode package new --binary <existing dll>`) *did* clear it: the error count
   dropped to 10, and our mod advanced to the next failure. So the gate is real and is
   cleared by the declaration.
2. **The cocos export removal.** Past the gate, the mod fails to load anyway:

   ```
   Failed to load binary for mod wawrzeniecjp.gd-ai-assistant:
   Unable to load the DLL: ERROR_PROC_NOT_FOUND
   The mod tried to access a function defined in another DLL, but the specified
   function was not found.
   ```

   A DLL built against the 2.2081 bindings cannot bind to the 2.209 game DLLs. Repackaging
   the manifest does not help, because this is an import-table failure at load time, not a
   metadata check — see "The real blocker" above for the three symbols responsible.

### The Geode v6 branch (investigated 2026-10-16)

`geode-sdk/geode` has a **v6** branch with 2.209 work in it, and it is the only place the
fix exists. Findings:

**It has what we need.** `update versions to 2.209`, `update 2.209 libs`, and
`add most of 2.209 header additions` are all on v6, along with `add new touch dispatcher
members` — the class our missing `registerForcePrio` belongs to.

**No v6 binary exists anywhere.** No release, no tag, no nightly (the `nightly` tag tracks
`main`, at `c1ca042`). Using v6 means **building the Geode loader from source**.

**Our porting cost looks low.** The largest v6 breaking change is `gd:: removal (wip)` and
our source contains **zero** `gd::` usages. `V6 event cleanup` is the other risk and has not
been assessed.

**The cheap path does not work, and the reason is precise.** The 2.209 bindings *did* turn
out to exist in `geode-sdk/bindings` (`bindings/2.209` and `bindings/2.2091`), and the SDK
exposes a supported override — its own default is guarded by
`if (NOT DEFINED GEODE_GD_VERSION)`, so a mod project can set it. Building with
`GEODE_GD_VERSION = 2.2091` therefore *did* switch Codegen to the 2.2091 bindings. It then
failed to compile, for instructive reasons:

- `2.209`'s `GeometryDash.bro` has an **upstream typo** — `= win = 0xd6920` (double `=`)
  at line 15705 — which Codegen rejects. `2.2091` is clean.
- `2.2091` then fails against the v5 SDK's cocos headers because the bindings need
  **matching headers that only v6 ships**: `scrollWheel` changed return type
  `void` → `bool`; new classes `CCKeyboardActionManager` and `CCTouchClipper`; several
  base classes absent.

That last point is the important one: **bindings and cocos headers are a matched pair.**
The bindings alone cannot bridge the gap, so v6 cannot be used piecemeal — it is the loader,
the headers and the bindings together, or nothing.

Worth recording the confirmed API deltas for the eventual port: `scrollWheel` returns
`bool` in 2.209, and there are new input classes. Neither affects the sidecar.

**Conclusion: waiting is the correct call, and nothing is lost by it.** Three loader builds
have been tried and none works, and the one branch that has a fix is an unreleased,
daily-changing WIP that must be built from source. When v6 releases, this is a rebuild-and-go
with minor porting — no design changes, because the blocker is the game's DLL and, after
that, a handful of Geode API renames.
All experimental state was reverted: `mod.json` is back to 2.2081, `CMakeLists.txt` carries
no version override, the third-party `cvolton.level-id-api` manifest was restored from a
backup, all 11 mods are back in place, and our installed `.geode` was rebuilt cleanly.
`git status` is clean.

### Timeline evidence (checked 2026-10-16)

- **Upstream issue: `geode-sdk/geode#2222` "Geode doesnt work in 2.209"**, opened the
  same day the update landed, labelled `bug`, `unverified`. The community noticed
  immediately.
- **Our installed loader is `413ac9b` ("bump ver", 2026-10-10).** Geode `main` is two
  commits ahead at `a6b9a91`, whose subject is
  **"disable unnecessary hooks in forward compat"** (2026-10-13, Cvolton).
  That is the same class of change as our crash, which is *inside* a forward-compat hook
  (`GLFWFocusCallback`).
- Geode's own update check still reports 5.11.1 as latest, so plenty of forward-compat
  work exists that has never been released.

So the wait is plausibly **days rather than weeks**, and a nightly loader may already
contain a relevant fix. `geode sdk install-binaries -v <VERSION>` can pin a build-time SDK
version, but it does not install a *loader* into the game; that is the official
installer's job, and it offers a nightly channel.


### Consequence for `mod.json`

The mod declares `"gd": { "win": "2.2081" }`. Do **not** add 2.209 until the game can
actually be launched and the mod verified against it -- a version claim that has never
been run is worse than an honest narrower one, because it turns a clear refusal into a
crash.

## 0.2c Level design layer, M2 in progress: S-L3 playtest control (2026-11-07)

`run_replay` exists in the mod (LEVEL-DESIGN-SPEC.md 7.2), with `scripts/check_replay.py`
as the spike. Three in-game runs; findings, all measured:

- **The tick is `GJBaseGameLayer::processCommands`**: 240 calls per second during the
  editor's playtest, a fixed `dt` of 1/240 s, no half-ticks, two per frame at ~120 fps.
  `m_gameState.m_currentProgress` advances by 2 per tick.
- **Order within a tick:** `PlayerObject::update(dt = 0.25)` (the move; GD's dt is in
  60 fps frames, so 0.25 is one 240 Hz tick), then `PlayerObject::postCollision`, then
  `processCommands`. A press injected with `handleButton` at the start of
  `processCommands` for tick N takes effect in the move of tick N+1: the first airborne
  sample is tick N+1.
- **Sample `getRealPosition()`, not `m_position`.** `m_position` is a copy that lags a tick
  and only catches up every other tick (x read 0, 2.596, 0, 2.596...). The first run
  sampled it and reported a false one-tick non-determinism.
- **Determinism: exact.** Five runs of one jump agree to 0.000 units in x and y (exit
  criterion 0.01).
- **1x speed: 311.58 units/s**, 1.29825 per tick, measured (it matches the commonly cited
  value). Tick rate 240, measured. Both recorded in `physics_constants.json`.
- **Cube jump, first look** (calibration proper is the next step): vy 11.18 at take-off,
  minus 0.216 per tick; y moves 0.225 x vy per tick, so gravity is exactly constant (second
  difference -0.0486); apex 63.85 units, airtime 103 ticks.
- **Death:** the editor stops its playtest **by itself** when the player dies, from
  inside the player's collision step (outside `processCommands`), and the reset clears
  the player. The replay watches the stop paths (`LevelEditorLayer::onStopPlaytest`,
  `EditorUI::onStopPlaytest` / `playtestStopped`): a stop during any part of the physics
  step, or with the player's dead flag set, is a death; between ticks it is the user,
  `CANCELLED`. Measured: death at tick 460, player x 597.2, so its right edge (+15) is
  612.2 against the spike hitbox's left edge at 615 - 3 = 612.
- **The game does not report the death's cause** for a hazard: `m_collidedObject` is null.
  The sidecar names it instead, from the death position and the level geometry, which
  the simulator needs anyway. So S-L3's "death object reported" is met on the sidecar
  side, not by the mod.
- **Bridge:** the reconnect backoff (up to 30 s) now sleeps in 250 ms slices, so a stop is
  seen promptly. A 5-6 s delay on closing GD was reported; the log shows Geode's shutdown
  completing in the same second, so it is not attributed to the mod.
- **Not done yet:** the on-screen "Assistant is testing..." label; `start_pos` starts;
  removing the probe instrumentation once calibration no longer needs it.

## 0.2b Level design layer, M1: GridText and schematic (2026-11-07)

Built and tested headless (277 sidecar tests). **Live test passed (2026-11-07):** in the
game, the model read a hand-built section with `render_grid`, rebuilt it with `place_grid`
(`replace`), and the second read was identical. M1's done-when criteria are met.
Sidecar only: the mod is unchanged, `place_grid` builds through the existing
`delete_objects` / `create_objects` requests.

- **`design/grid.py`:** GridText parse/emit, the built-in legend (every default id is
  checked against `roles.json`), custom legends, hitbox footprints for multi-cell objects,
  reading from the snapshot and planning placement. Property test: `emit(parse(g)) == g`
  (hypothesis, 300 cases); fixture test: a written grid reads back as the same text.
- **Tools:** `render_grid` (also accepts `cols`/`rows` in grid cells, and returns
  `legend_for_place_grid` when custom characters appear), `place_grid` (`add` refuses
  occupied cells, `replace` rebuilds the rectangle, `preview`, per-turn cap and the turn's
  transaction; `verify` waits for M2), `render_schematic` (hitbox diagram, `grid` overlay;
  the others report the milestone that brings them).
- **Prompt:** a GridText section; the ground paragraph now gives y = 0 (S-L1) instead of
  the `lowest_y` workaround.
- **Roles:** the start position (id 31) is typed by the game as a trigger-flagged Modifier;
  it is now `start_pos`, by id.
- **S-L9, measured** (`scripts/check_snap.py` and a read of the test level, 2026-11-07).
  Placed by a normal click, offsets from the cell centre: small spike (39) y -9, yellow
  and pink pads (35, 140) -13, blue pad (67) -12, the orb (36, control) 0, and **half
  slabs at the top of the cell**: planks (40, 1903, 147, 1904, 215) +8, the 15-unit
  outlined half block (662) +7.5. So the editor's natural slab is `-` (+8); `=` (bottom)
  is -8, derived from the plank's hitbox. Offsets are per character (`_SNAP_BY_CHAR`).
- **The editor's in-cell offset is per object, not geometry.** Planks are flush with their
  hitbox (+8, where the sprite would give +8.25), the thin "perspective outline" blocks
  (534, 525: 10-unit sprite, 2-unit hitbox) are flush with their sprite (-10), and the
  small spike matches neither (-9). Custom legend entries can therefore say
  `"align": "top"` (or bottom/left/right): flush with the hitbox edge, which covers the
  common half blocks exactly. On read an aligned match must be exact (+-0.5); anything
  else stays `?`.
- **Found in the test level:** 17 spikes at x = 30c, on cell *boundaries* rather than
  centres, probably from early programmatic placement. They read as `?` (-15 units off),
  which is correct; an early version of the alignment fallback accepted them as "flush
  left" within the +-3 centre tolerance, hence the exact-match rule.
- **Dependencies:** Pillow (renderer, planned by the spec), hypothesis (dev).

## 0.2a Level design layer, M0 (2026-11-07)

Foundations for docs/LEVEL-DESIGN-SPEC.md, built and unit-tested. The first in-game check
(`check_foundations.py`) ran on 2026-11-07 and found three bugs, fixed below. The
confirmation run (`check_m0_fixes.py`, same day) passed every check and measured S-L1.
The full role dump ran the same day. Still to do: a snapshot timing on a large level.
See docs/M0-in-game-checks.md.

- **Protocol (additive to v1):** `get_level_objects`, `describe_object_types`, `revision` on
  `get_editor_state` and `level_changed`, and error codes `UNSUPPORTED_REQUEST` and
  `LEVEL_CHANGING`. The mod now answers unknown request types with `UNSUPPORTED_REQUEST`
  instead of `INVALID_REQUEST`.
- **Level revision (mod):** a per-frame fingerprint (object count, undo/redo list sizes and
  newest entries), plus explicit bumps from the mod's own mutating jobs. The editor's
  undo/redo entry points are inlined on Windows and cannot be hooked.
- **Sidecar `design` package:** measured constants with provenance, grid coordinates,
  the role table (from the game's `GameObjectType`, with a flagged frame-name fallback), and
  the per-turn level snapshot with its spatial index and cache.
- **Fake editor** (`sidecar/evals/fake_editor.py`): in-memory, schema-validated in both
  directions, used by the new agent-level tests.
- **Tool hiding:** a request answered with `UNSUPPORTED_REQUEST` withdraws every tool that
  depends on it for the rest of the connection.
- **Y coordinates were off by +180 (bug, fixed).** Protocol `y` minus save key `3` measured
  +180 on all 28 objects: `cocosYToLevel`/`levelYToCocos` had the 90-unit offset with the
  wrong sign. Every `y` the model read or wrote was six blocks above the level coordinate;
  relative placement worked, which is why it went unnoticed. Protocol `y` is now the
  save-string `y`. The camera (`get/set_camera`, capture viewports) reported raw cocos y
  and now uses level y too (LEVEL-DESIGN-SPEC §4.1).
- **`describe_object_types` skipped object setup (bug, fixed).** `createWithKey` only builds
  the sprite; type, hitbox size and radius are set by `customSetup`, which the job now
  calls. Before, a spike (id 8) reported type 0 with a 30x30 box.
- **Edit Object was invisible to the revision (S-L10, fixed).** A colour change touches
  neither the object count nor the undo lists. The fingerprint now also hashes the selected
  objects' save strings (capped at 64, every 8 frames); selecting or deselecting only
  re-baselines it. All other S-L10 edit paths (place, move, rotate, duplicate, delete,
  undo, redo, the assistant's own edits) moved the revision, and pan/zoom did not.
- **Dead sidecar not noticed (bug, fixed).** On Windows a force-killed sidecar did not wake
  the mod's pending receive, so the mod never reconnected. The watchdog now pings every
  ~3 s; the failed send ends the session and the reconnect loop takes over. The dev-script
  harness also now fails fast when the port is taken instead of waiting forever.
- **S-L1: the ground top is y = 0** (measured). A default block resting on the ground reads
  y = 15 in both the protocol and save key `3`. Recorded in `physics_constants.json`; the
  scripted demo's fallback row moved from 105 to 15. One level so far.
- **Confirmed after the fixes:** a spike describes as Hazard with a 6x12 hitbox, the saw as
  Hazard with radius 32.3, orbs, pads and portals with their own types; protocol y minus
  save y is 0; Edit Object moves the revision; the camera reports level y. A
  deselect/reselect step also moved it, but the log shows a block was placed and removed
  in that step (a click on empty space in the Build tab), so that was a real edit.
- **S-L2: off-scene description is safe for every id.** `dump_roles.py` described all
  4093 catalog ids with no crash and no refusal; `data/roles.json` is committed. By role:
  deco 3257, solid 336, collectible 147, trigger 129, hazard 99, slope 74, orb 12, special
  10, portal_mode 8, portal_other 6, pad 5, portal_speed 5, portal_gravity 3, portal_size 2.
  The game types speed portals (200-203, 1334) and the camera/BPM guides (2016, 3642) as
  the generic `Modifier`, so `roles.py` tells them apart by id, only while the game still
  reports `Modifier` for that id. The remaining specials (breakable block, the
  collision-changing and force blocks, the collision block, the checkpoint) are correctly
  things the simulator must not judge yet.
- **Snapshot timing: not yet meaningful.** 28 objects in 17 ms is one round trip, not a
  rate; the 3 s / 10k target needs a level with thousands of objects.

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

## 0.1b Ground placement: three wrong answers, then a workable one (2026-10-31)

Ground placement is the one thing in this project that has been wrong in four different
ways, so the sequence is worth recording — the final answer is a small idea that only
looks obvious after the others failed.

1. **A hard-coded constant** (`y=105`) in the demo provider. Correct in one level, buried
   under the ground in the user's. The objects were created, reported, and invisible —
   indistinguishable from a failed edit.
2. **`GJGroundLayer::getGroundY()`.** Sounded authoritative; measured **0**.
3. **The ground layer's node position.** Also wrong, and worse than a constant because it
   *looks* like a measurement: logged `102.586`, then `153.1`, then `201.27` on the same
   level. It tracks the camera, so it moves as you scroll. Reporting it would have had the
   model place objects wherever the view happened to be.
4. **`lowest_y` from `list_object_types`** — the y of the lowest object actually in the
   level. Stable, observable, and exactly what "on the ground" means in a level that has
   ground content. The prompt tells the model to use it and to expect no `ground_y`; an
   empty level has no `lowest_y`, and the model is told to place at 0 and say so.

`ground_y` is now **deliberately not reported at all**. A missing field is honest; a moving
number presented as a constant is not.

The general lesson is the same one the scale setter taught (§ docs/save-string-keys.md):
an API existing, being named plausibly, and returning without error says nothing about
whether it means what it appears to. Two of the three failures here returned clean values.

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

**What is actually implemented (0.11.0), and why it differs.** S4 was never done, and hooking the editor's mutation paths is invasive, so `level_changed` comes from the poll the drain already performs — one `CCArray::count()` per frame. It reports **structural** changes (creations and deletions), not a bare move or rotate, and never a bounding box. That is the right trade for the problem it solves: the damage from stale context is the model believing objects exist that the user has deleted.

Two details that are easy to get wrong, both found by testing:

- **The baseline must update during a transaction too.** Skipping it while a transaction is open leaves the baseline at its pre-edit value, so the turn's own edits get reported as the user's the moment the transaction closes — the exact opposite of the intent. A transaction absorbs its changes silently instead.
- **A one-shot "already reported" flag loses information.** Add two objects, delete them again, and with a flag the deletion is swallowed. Comparing against the last *reported* count with a 250 ms window reports a later edit eventually, which is why the flag was replaced. Verified: add, add, delete produces `+1`, `+1`, `-1`.

`selection_changed` is still **not** emitted. The assistant discovers selection through `get_editor_state`.

The sidecar does not act on `level_changed` mid-turn — it would interleave with the turn's own events. It marks the history stale, and the next turn opens with a note telling the model its picture may be out of date and to re-query (see `agent.History`). The same marker is set by `transaction_reverted`, since a Ctrl+Z invalidates earlier reasoning just as a hand edit does.

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

**Loop limits (configurable, §10.2):** `max_tool_rounds` (default 40), `max_objects_per_turn` (default 5000, mirrors the mod limit), per-turn token budget, wall-clock timeout per turn. On hitting a limit the turn ends with `status: "incomplete"` and the assistant states what was and wasn't done.

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
| `tool_call` | `{ turn_id, tool, summary? }` | Names the tool the turn is running, for the thinking indicator. Sent before each call. |
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
| `list_object_catalog` | `{ offset?, limit? }` | `{ entries: [ { id, frame?, grid_size? } ], total, next_offset }` | The game's own object table, read from `ObjectToolbox` (§7.5.1). Paged; `next_offset` is null on the last page. Does not require an open editor. |
| `ping` | `{}` | `{ pong: true }` | Health check. |
| `get_level_objects` | `{ cursor?, limit? }` | `{ objects: [ { object_id, id, properties, save } ], total, revision, next_cursor }` | Whole level, paged, revision-guarded. docs/LEVEL-DESIGN-SPEC.md §4.3. |
| `describe_object_types` | `{ offset?, limit? } \| { ids }` | `{ entries: [ ObjectTypeDescription ], total, next_offset, failed }` | Each type instantiated off-scene and described. docs/LEVEL-DESIGN-SPEC.md §4.2. |

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

### 7.5.1 The Object Catalog Is Read From the Game

The name-to-id table this system needs already exists inside GD, and it is not
documentation or a community list — it is the table the game itself uses to decide
which sprite an id draws. `ObjectToolbox` (`sharedState()`) holds it: `m_allKeys` maps
id → sprite frame, `intKeyToFrame(id)` is the game's own lookup, and
`gridNodeSizeForKey(id)` reports the id's grid footprint. `list_object_catalog` reads
that, so **every entry it returns is real**, which is the only property that matters
here: a guessed id does not fail loudly, it places the wrong object in the user's level.

Consequences worth knowing:

- Frames are the vocabulary. Ids have no authoritative names, but their frames do:
  `spike_01_001.png`, `pit_03_001.png`, `sawblade_02_001.png`, `block001_slope_01_001.png`.
  The sidecar derives a readable name and a category from the frame (leading numeric
  runs and GD's `_001` suffix dropped), and searches against the frame text as well as
  the derived name.
- `grid_size` is the size signal, and it is what makes descriptions like "thin",
  "small" or "tiny" answerable. Those words name a size, not an object, so no entry
  matches them; the search returns the objects that matched the rest of the query and
  reports the unmatched words, leaving the model to choose by `grid_size`.
- Types drawn programmatically (most triggers) have no frame. They are still listed,
  with an id and no name, because "this id exists" is worth knowing even when it
  cannot be described.
- Paging, not one frame. The table holds a few thousand ids, and one response would be
  both wasteful and close to the protocol's 1 MB limit. `next_offset` is explicitly
  null on the last page, so a mod that never reports the end is distinguishable from
  one that has finished.
- Fetched **lazily**, on the first `search_object_catalog` of a process, and cached for
  the process's lifetime. Most turns never look an id up, and paying a handful of round
  trips at the start of every turn would slow down "delete the spikes" for nothing.
  Failure is bounded to three attempts, so a mod that does not implement the request
  does not cost a failed round trip forever.

The sidecar still merges a curated seed and an optional user catalog file *over* this
table. The game supplies the ids; the curated entries only add presentation (a friendly
name, aliases) for ids they name. Neither source guesses about the other.

### 7.5.2 Colour Channels (measured, working)

Colour is **level** state, not object state: an object carries a channel id
(`color_channel_1`/`_2`, save keys 155/156) and the channel holds the colour. Objects
sharing a channel always share a colour, so "recolour this object" means either
changing the channel (affecting every object using it) or repointing the object at a
different channel. `get_color_channel` and `set_color_channel` do the former.

**System channels are not a separate namespace.** The custom channels are 1-999 and the
system ones are 1000 and up -- BG 1000, ground 1001, ground line 1002, middleground
1004+ -- all in the same id space, all settable the same way.

#### Two effect managers, and only one of them is storage

`LevelEditorLayer::m_effectManager` is the **live** one: it drives rendering and is
rebuilt from the level's own copy every frame. `LevelSettingsObject::m_effectManager` is
that copy, and it is what `LevelSettingsObject::getSaveString()` serialises into the
`kS38` section. `LevelEditorLayer::transferDefaultColors(src, dest)` exists precisely
because there are two: a level load copies between them.

Writing to the live manager therefore cannot work, and four implementations failed the
same way before this was understood -- each reported the new colour back from the object
it had just written, and each was undone **within one frame**:

```text
set to (0,0,0)  ->  reads back as (0,0,0)
next frame:     from=(255,255,255) color=(255,0,0)   <- the original
```

The write path is: build the channel's `kS38` entry, `setupFromString` it into the
**stored** action, `transferDefaultColors(store, live)` to push it into the live manager,
then re-tint the objects that use the channel. Re-tinting is per object via
`manualUpdateObjectColors`; `updateObjectColors` takes a required `CCArray*` and crashes
on null.

#### `ColorAction` is an animation, not a colour

Its API is the giveaway: `step(float dt)`, `m_deltaTime`, `m_duration`, `m_stepFinished`,
`m_fromColor` -> `m_toColor`, and `m_paused`. `m_color` is the interpolated **frame
output**, which `step` recomputes -- which is why writing it was never going to persist,
and why a `ColorAction` pointer differs between requests: these are transient.

`m_fromColor` is the animation's *start* colour, not the stored one. Reporting it made
every channel read as white and made the user's own colour edits invisible to the model;
`m_color` is the field to read.

#### The `kS38` entry format

Measured from `LevelSettingsObject::getSaveString()`:

```text
kS38,1_<r>_2_<g>_3_<b>_11_<..>_12_<..>_13_<..>_4_<player>_6_<channel>_5_<blend>_7_...|...
```

So key 1 is red, 2 green, 3 blue, **6 the channel id**, 5 blending, and 4 the player
colour (1 or 2) an entry copies from -- which is what "custom colour 1/2" in the editor
refers to, and is not a channel. Entries are `|`-separated, and the section ends at the
next `,k`.

### 7.5.3 The Save-String Property Table (generated)

The mod reads and writes a fixed set of eleven named properties (§7.5). Every object
also carries far more than that -- a move trigger's duration and easing, a pulse
trigger's target colour, a portal's target channel -- reachable only by **save-string
key**. Without a key-to-meaning table the model can only guess, and a guessed key
silently edits the wrong field.

**The table is generated, not written.** `sidecar/scripts/gen_properties.py` parses
the Geode bindings, which annotate members with the key they serialise under:

```cpp
// property 51
int m_targetGroupID;
```

```text
python scripts/gen_properties.py \
    ../build/_deps/bindings-src/bindings/2.2091/GeometryDash.bro
```

Output: `sidecar/src/gd_ai_assistant/data/properties.json`, committed, so the sidecar
has no build-time dependency on the bindings. From 2.2091: **48 classes, 512
properties, 511 distinct keys**, 45 KB.

**Cross-checked against measurement.** The generated table agrees with every key
observed directly in the editor: 24/25 = `zLayer`/`zOrder`, 57 = `groups`, 51 =
`targetGroupID`, 128/129 = `scaleX`/`scaleY`, 155/156 = the colour indices. That
agreement is asserted by a test, so a generator that silently stops parsing fails
rather than producing a plausible-looking file.

#### What the bindings do not annotate

`x` and `y` have no annotation: the members (`m_positionX`/`m_positionY`) carry no
`// property` comment, even though they are the two properties that matter most. They
are added from measurement (`1,<id>,2,<x>,3,<y>` in every observed save string) and
recorded in the file under `supplement`, with its source, so the one non-generated
part is visible rather than hidden.

Keys 4-9 are also unannotated. A colour trigger's save string shows `7,255,8,255,9,255`
for its RGB but nothing measured here distinguishes 4, 5 and 6, so **they are left
out**. A missing key makes the model say it does not know; a wrong key makes it
confidently edit the wrong field.

#### A multi-key pitfall, recorded because it cost real time

One annotation can cover two keys:

```cpp
// property 28, property 29
cocos2d::CCPoint m_moveOffset;
```

The first version of the generator matched `//\s*property\s+(\d+)`, which requires a
`//` before *each* key and therefore found only 28. Every `CCPoint` member lost half
of itself, so `x`/`y`, move offsets and scale pairs were all absent from a table that
looked complete. Detection and extraction are now two patterns: one to recognise the
annotation, one to pull every bare `property N` out of it.

#### Lookups

Two questions, answered differently, because the class hierarchy is not always known:

- **By key**, globally. The save-string vocabulary is global and additive -- a key
  means the same thing on every object that has it -- and the generator confirms it:
  zero conflicting keys across 48 classes. This is what makes reading an object's raw
  save string meaningful without knowing its class.
- **By name**, optionally class-scoped. Inheritance is resolved on the fly by walking
  the parent chain (nearest definition wins, as the game's serialiser behaves), so a
  `RingObject` reports its 152 properties including the inherited scale and colour
  keys. Expanding inheritance into the file would treble it and invite copies to drift.

Tools: `search_properties` (`query` for a name fragment, or `key` for a number) and
`describe_class` (everything a class has). Both are sidecar-local -- no editor round
trip, and no engine needed.

**The table describes; it does not enable writing.** Only the eleven named properties
are writable, so the prompt tells the model to look a key up, report what it is, and
say the setting has to be changed by hand rather than claim a change it cannot make.

### 7.5.4 Writing Any Property (`get_save_properties`, `set_save_property`)

§7.5 names eleven writable properties. Every object also carries dozens more -- a
spike's "no touch" flag, a move trigger's duration and easing, a pulse trigger's target
colour -- reachable only by **save-string key**. `set_save_property` writes them: read
the object's save string, change one key, and let the game rebuild the object from it.

**The object is replaced, and its unique id changes.** That is not an implementation
shortcut. The save string is the only interface that reaches those fields, and the game
can only turn a save string into an object by creating one. Because it is unavoidable
it is also made unmissable: the result carries `ids_changed: true` and a `replacements`
list of `{old_id, new_id}`, the turn keeps that mapping, and any later call using a
stale id is **remapped automatically**. A model that kept using the old id would get
`OBJECT_NOT_FOUND` for an object it can see in its own earlier results -- a dead end it
cannot diagnose. The remap only ever follows a reported id; it never invents one.

Keys 1, 2 and 3 (type, x, y) are refused **on the sidecar side**, before the round trip,
and the refusal names `modify_objects`. Position especially: the save string stores
level Y while every other interface uses Cocos Y, so a write here would silently move
the object 90 units.

#### Resolving a name to a key

The member names are not guessable. The flag a user calls "no touch" is
`m_isNoTouch`, and neither "no touch" nor "nontouch" is a substring of it. Resolution:

1. lowercase and strip every non-alphanumeric -- `no touch` and `no-touch` both become
   `notouch`, which *is* a suffix of `isnotouch`;
2. one spelling variant, `non` -> `no`, so `nontouch` reaches the same place;
3. rank matches: exact, then prefix **or** suffix (equally -- the game spells flags
   `isX` and qualifiers `xType`, so preferring one breaks the other), then substring,
   tie-broken by the name that adds fewest characters to the query.

Ambiguous names report their alternatives rather than picking one, because the mod does
not report object classes and guessing would be silent.

#### Values are text

Save strings have their own encodings: booleans are `0`/`1`, group lists are
dot-separated, HSV is `a`-separated, text is base64. The sidecar encodes what it can
(a boolean becomes `1`/`0`, because `true` reads back as zero -- a wrong value rather
than an error) and passes anything else through as literal text. The mod does not
re-encode: it stores the string and lets the game's parser interpret it.

#### Revert works

The rewrite is recorded in the open transaction as a delete of the original plus a
creation of the replacement, which is what happened, so the turn's Revert restores the
original object with its original properties. Prior property values recorded for the
old id are dropped at the same time (`forgetModified`), or the restore would log a
spurious "not found" for an object it is about to recreate.

`get_save_properties` reads an object's whole set, named from the table in §7.5.3. A key
the table does not know is reported with `name: null` rather than omitted -- "this
object has a property I cannot name" is useful, and dropping it would make the object
look simpler than it is.

### 7.5.5 Level Settings (one request)

Level-wide settings live on one `LevelSettingsObject`, the game applies them together,
and nobody changes them often -- so they are **one request with every field optional**
rather than one request per field. `get_level_settings` and `set_level_settings`.

| Field | Notes |
| --- | --- |
| `background`, `ground`, `ground_line`, `middle_ground` | Pattern indices, not names. |
| `start_mode` | Gamemode the level starts in. |
| `start_mini`, `start_dual`, `two_player` | |
| `platformer`, `reverse`, `rotate_gameplay`, `mirror` | |
| `song_offset` | Seconds between the level start and the song. |
| `song_type` | **Read-only.** Which catalogue the song comes from. |
| `audio_track` | Writable only on the normal catalogue. |
| `song_name` | Read-only, when the game can resolve it. |

#### Applying a change is the game's job

`LevelEditorLayer` implements `LevelSettingsDelegate`, and `levelSettingsUpdated()` is
what the editor's own settings popup calls when it closes. Writing the fields and then
calling that one virtual re-creates the background, ground, ground line and middleground
and re-applies the colours. Recreating them here instead would mean reproducing node
positions, scales and draw order the game already gets right -- which is the same
lesson as the colour work: prefer the game's own path over poking its state.

#### The song is two values, and the names mislead

`GJGameLevel::m_songID` is **not** a Newgrounds id. It is the catalogue, matching
`SongSelectType` (`Default = 0`, `Custom = 1`, `Default2 = 2`), and `m_audioTrack` is
the index into that catalogue. The first implementation assumed a Newgrounds id from the
field name and was wrong about both. Neither field carries a `// property` annotation,
so the generated table could not settle it either -- it was measured live.

The custom catalogue means browsing, searching and downloading from an external library.
That is a feature of its own, and it fails silently on a bad id, so `song_type` is
**refused** on write and `audio_track` is refused when the level is on a custom
catalogue -- an index into a catalogue with no fixed ordering would appear to succeed
and change nothing audible.

#### Two rules worth keeping

- **Validate before writing.** A request with one bad field changes nothing at all,
  rather than half-applying and reporting failure.
- **Refuse unknown keys.** Ignoring one reports success for work not done, which is the
  failure mode this project keeps having to design against. `song_type` gets a named
  refusal rather than a generic one, because it appears in the *result* and assuming it
  is therefore writable is a natural mistake.

Settings are level state, not object state, so a write is not recorded in the turn's
transaction and Revert does not undo it. The result says `revertible: false`, and the
prompt tells the model to mention it -- briefly. An earlier version warned about it at
every opportunity, which made the model refuse to change settings the user had asked
for; settings are ordinary edits and deserve no more caution than the rest.

### 7.5.6 Seeing the Level (`capture_screenshot`)

Renders the editor view to a PNG so the model can look at it. **On demand only**: a
capture costs a vision-capable call, and no turn needs a picture by default.

| Payload | Meaning |
| --- | --- |
| `region: "screen"` | The whole editor view. |
| `region: "selection"` | A box around the selected objects, padded by `radius` points (default 120). |
| `include_ui: true` | Also capture the editor's chrome. Off by default. |

The result reports the captured area twice, because the two are different numbers:
`region` is the crop in **screen points** (what the renderer needed), and `viewport` is
the same area in **level coordinates** (what a caller comparing the image against object
positions needs).

The region is what makes this useful rather than merely possible: a 512x320 view of a
detailed level is mostly noise at any resolution a model can take in, while a box around
the objects just edited is legible.

#### A file path, not base64 through the protocol

A 512x320 PNG is often 100-200 KB and base64 inflates it by a third, against a 1 MB
frame limit -- workable today and fragile the moment anything changes. Both processes run
on one machine by construction, so the file is the natural shared medium, and it needs no
base64 encoder or PNG plumbing in the mod: `CCImage::saveToFile` is the game's own
writer. One fixed name under the mod's save directory, rewritten each capture, so nothing
accumulates.

Rendering uses `CCRenderTexture`, cropped by drawing through a sprite with a texture
rect. The running scene is deliberately **not** shifted to fake a camera move: that is a
one-frame lie about the scene's position, and a crash in between would leave it shifted.

#### Images ride on a user message

They cannot ride in the tool result. Providers document images in **user** messages and
reject them in system and assistant ones, and a tool result has a role of its own the
documentation does not cover. So a capture is queued and attached to the next model call
as its own user message, with a line saying what it shows.

The queue is **drained**, not kept: history holds only user and assistant text, and a
retained screenshot would be a picture of a level state that has since changed -- the
same reason tool results are dropped when a turn ends.

#### A failed capture says so

A model handed nothing will still describe the level from memory, which is the exact
outcome this feature exists to prevent. So the tool result says "you have NOT seen the
level", and `capture.py` refuses files that are missing, oversized (a request error, not
a detailed level) or implausibly small (a blank PNG) rather than sending anything at all.

#### The chrome is hidden, not cropped

The chat panel is always hidden for a capture, and from 0.14.0 so is `EditorUI` itself
unless `include_ui` is set. Hiding a node and rendering the scene is exact; cropping a
guessed inset is not, and the first live test had the model reporting on toolbars instead
of the level. `EditorUI` is the parent of the panel, so one flag hides the whole
interface -- there is no separate panel flag to keep in sync.

### 7.5.7 Looking Around (`get_camera`, `set_camera`)

A capture shows whatever the view happens to show, which is not necessarily what the
model needs to see. Two requests move it:

| Request | Payload | Result |
| --- | --- | --- |
| `get_camera` | `{}` | `{ camera: { x, y, zoom, visible } }` |
| `set_camera` | any of `x`, `y`, `zoom` | `{ camera: { x, y, zoom, visible } }` |

`x`/`y` are **level** coordinates of the centre of the view, the same space objects live
in, so they can be taken straight from `query_objects` results. `zoom` is clamped to
0.05..4 rather than refused: it is a view setting with an obvious "as far as it goes"
reading, the opposite of a property write, where clamping would silently do something
other than what was asked.

#### `visible`: which part of the level is on screen

`visible` is the rectangle of the level currently in view, as `x1, y1, x2, y2` in level
coordinates. The centre and the zoom *imply* it, but only to a caller that already knows
the window size and the height of the visible band, which it has no way to learn -- so it
is reported rather than left to be reconstructed. This is the field that makes a picture
usable: it turns "there is a gap near the right edge" into "there is a gap near x=1200",
and lets a caller check whether something is on screen *before* spending a capture.

`get_editor_state` carries the same camera object, and `capture_screenshot` carries the
rectangle as `viewport`. One shape, three messages: the schema `$ref`s a single `Camera`
definition rather than repeating the fields.

The `camera` block in `get_editor_state` was declared in the schema from early on and
**never emitted** -- an earlier note recorded it as unreadable for the same reason
`ground_y` is, since the only obvious source was the ground layer, which tracks the camera
rather than describing the level. The transform-based reading above is what made the field
honest. It is omitted, not zero-filled, when it cannot be read: a camera at `(0, 0)` zoom
`1` is a plausible-looking lie that a caller would plan around.

#### Reading position off the layer's transform

There is no `getCameraPosition`. The editor scrolls by moving `LevelEditorLayer`'s object
layer, so converting the screen centre into that layer's node space answers "which level
point is the middle of my screen showing" directly -- and stays correct at any zoom,
whereas the obvious "negate the position and add half a window" only holds at 1x, because
zoom scales the layer as well as moving it.

**Zoom is measured the same way, from the same transform.** The first version reported
`EditorUI::m_editorZoom` and reported `1.0` for every zoom in the live test while the view
visibly zoomed -- the field is either not what `updateZoom` writes or not the zoom. Feeding
a full window's width through `convertToNodeSpace` and taking `width / span` cannot lie,
since the transform is the thing doing the scaling, and taking both readings from one
transform means position and zoom can never disagree. It also does not matter which node
in the chain carries the scale: `convertToNodeSpace` walks the parent chain, so a scaled
ancestor counts exactly like a scaled layer.

#### Panning uses the editor's own primitive

`EditorUI::centerCameraOnPoint` is the natural call and is **uncallable**: the bindings
mark it `TodoReturn` (an incomplete return type with a private destructor), because the
real signature is `void` and PIE had nothing to infer from. `moveGamelayer(CCPoint)`,
`constrainGameLayerPosition()` and `updateZoom(float, CCPoint)` are all bound as `void`
and are what the editor's own drag and zoom handlers use, so panning is a world-space
delta to `moveGamelayer` -- measured in screen points, since at 4x the level travels four
times as far per point -- followed by a constrain to the level bounds.

The result is **measured again after the write**, not echoed: a clamped zoom or a pan
stopped by the level edge then shows up in what the caller sees. Because zoom is measured
rather than echoed, `zoom: 2` coming back as `1.9` is the editor's own limit, not a
miscalculation.

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
| `UNSUPPORTED_REQUEST` | The mod does not implement this request type (it is older than the sidecar). The sidecar hides the tools that depend on it for the rest of the connection. |
| `LEVEL_CHANGING` | A `get_level_objects` page or cursor straddled an edit; read again from the start. |

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
| States | Expanded, collapsed (title-bar chip), hidden (nothing drawn at all) |
| Toggle | Geode keybind `toggle-chat`, default Ctrl+Shift+C |
| Hide | Geode keybind `hide-chat`, default Ctrl+Shift+H. Removes the panel completely, header included; a collapsed panel still leaves a strip over the level. |
| Playtest | Hidden automatically while the level is being tested, and restored afterwards. The two reasons to hide are separate flags (`m_hidden`, `m_playtestHidden`) and visibility is derived from both, so leaving playtest reveals the panel only if the user had not hidden it themselves. |
| Lifetime | Created with the editor layer, destroyed when it closes |
| Touch | Claims the **message area, the input row and the draft preview**, at priority `-1`. Lower values dispatch first, so a lower value steals scroll drags from the panel's own `ScrollLayer`; `-1` sits behind the children and ahead of the editor layer at `0`. The input row is claimed because the text field only claims its own text, so a click beside it used to reach the editor and press a button underneath the panel -- Send and the field still work, because `kCCMenuHandlerPriority` (-128) dispatches first. |
| Hidden | Removed from the touch dispatcher entirely, not just hidden. `setVisible(false)` stops drawing but leaves a targeted delegate registered, so a hidden panel still ate clicks over the editor -- most visibly after Ctrl+Shift+H, where a corner of the screen stopped responding. `applyVisibility` toggles `setTouchEnabled` with the same flag that drives visibility. |

## 8.2 Layout

```text
┌─────────────────────────────────────────────┐
│ ● LLM Assistant              Revert   [–]   │  ← header: status dot, revert, collapse
├─────────────────────────────────────────────┤
│                                             │
│   [Message scroll area]                     │
│                                             │
│   You: Add a row of spikes                  │
│                                             │
│   Assistant: I'll add 5 spikes...           │
│                                             │
│   [icon] Finished with 2 actions            │
│                                             │
├─────────────────────────────────────────────┤
│ [ Type your message...        ] [Send/Stop] │
└─────────────────────────────────────────────┘
```

**Revert is in the header, not on the summary line** — see §8.4.1. An action at the end
of a scrolled area is only reachable when the scroll happens to land on it.

## 8.3 Message Display Area

| Property | Value |
| --- | --- |
| Component | Scroll layer containing a stack of message nodes |
| Text renderer | `geode::Label` with `chatFont.fnt`. **Not** `bigFont.fnt`: it is size 32 and draws lowercase as capitals, so body text becomes an all-caps wall at double height. `chatFont.fnt` is size 16 with real lowercase. Markdown is **not** rendered (`MDTextArea` unused); fence lines are stripped so they are not shown literally. |
| User messages | Right-aligned, distinct background |
| Assistant messages | Left-aligned, default background |
| System messages | Centered, muted, smaller font |
| Auto-scroll | To bottom on new message. Done through the scroll layer's own `scrollToFromTop`, never by writing the content layer's position: it is a `GenericContentLayer`, whose `setPosition` overrides the y it is given whenever the content is shorter than the view, so a direct write lands somewhere other than where it was asked to go. |
| Layout | Manual, in `ChatPanel::refreshLayout`: measure each child's **scaled** height, size the content layer to `max(viewport, sum + padding)`, then place children bottom-up (newest last). No `Layout` object, and visibility is never consulted while measuring -- the content layer culls off-screen children by setting their `visible` flag, so measuring only the visible ones makes the transcript shrink as it is scrolled and repack messages on top of the ones it skipped. |
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
| Action summary | Left-aligned compact bubble with sprite icon | Tap to expand; the turn's **Revert** action appears in the panel header, not here (see below) |
| Error message | Left-aligned, red-tinted bubble | Human-readable text; code in smaller print |
| System notification | Centered, muted | "Connected", "Disconnected", "Reconnecting…" |

### 8.4.1 Revert

**Revert lives in the panel header**, left of the collapse arrow, and only while the
newest turn opened a transaction. It is the only way back from a turn's edits: they
never reach the editor's undo stack, so Ctrl+Z undoes the user's own last edit instead
of ours (D2).

It was first built inside the summary bubble, on the same line as the summary text, and
that placement does not work. The summary is the **last thing in a scrolled area**, so
an action there is reachable only when the transcript is scrolled to its exact bottom —
and a safety net that is invisible when you need it is not a safety net. The row also
had to be a container node holding the label and the button, and a container sized from
one label's height behaved badly: the log showed the row correctly placed at the bottom
of a correctly scrolled transcript (`content=216 view=124`, `content y=0`, row at
`y=22` of `h=20`, `visible=true`, world y two points above the clip floor) and the user
still saw only a sliver of its first line. Moving the action to fixed chrome removed
the container, and with it the fault.

Behaviour:

- Revealed by `turn_finished` when `txn_id` is non-empty. A read-only turn never shows it.
- Disabled on press, before the work happens, so a second press cannot revert a
transaction that is already gone.
- Dropped on use, on failure, and when the next turn's summary arrives. Reverting an
  older turn is not merely stale — its inverse would apply on top of changes that assume
  the newer turn's edits are still present, which is what `TRANSACTION_NOT_REVERTIBLE`
  exists to refuse.
- Reports its outcome into the transcript: a system line on success, an error line on
  refusal, with the protocol code stripped (the code is in the log; a player pressing a
  button should not be shown `TRANSACTION_NOT_REVERTIBLE`).
- On success the mod sends `transaction_reverted` (§7.4) so the model learns its work is
  gone. **Ctrl+Z reports nothing**, because it does not undo our edits at all.

## 8.5 Input

| Property | Value |
| --- | --- |
| Component | `geode::TextInput`, with `setCommonFilter(CommonFilter::Any)`. The default filter rejects punctuation including `!` and `'`. |
| Placeholder | "Ask about this level..." |
| Send | Enter or Send button |
| Max length | `max-message-length` (default 2000) |
| While processing | Input disabled and greyed; the Send button becomes **Stop** (sends `cancel_turn`) |
| Multi-line | No -- `TextInput` has no multi-line mode. A **wrapped preview** above the input mirrors the draft at a fixed size, shown only once the draft would make the field shrink its own label. `TextInput` scales the label down as soon as the text exceeds `m_maxLabelWidth`, so the field is unreadable well before the text overflows; the threshold is therefore *when the input starts shrinking*, not when the text runs out of room. Capped at three lines so a pasted wall of text cannot swallow the transcript, and the space comes out of the message area rather than growing the input row -- growing it would re-measure the whole panel on every keystroke. |
| Focus behaviour | Enter is caught in a `$modify` hook on `EditorUI::keyDown` (S6). While a GD text field has focus, keyboard events do **not** reach a mod layer at all, and `TextInput` does not forward `enterPressed`; see §0.1. |

## 8.6 Keyboard

| Shortcut | Action |
| --- | --- |
| Ctrl+Shift+C (rebindable) | Toggle panel expanded/collapsed |
| Ctrl+Shift+H (rebindable) | Hide the panel entirely, or bring it back |
| Enter | Send message, when the input has focus. Routed via `EditorUI::keyDown`; see §0.1. |
| Escape | **Not implemented.** Input focused: unfocus. Otherwise: collapse the panel. |
| Wheel | Over the transcript: scroll the transcript, not the level. Implemented in a `$modify` of `CCMouseDispatcher::dispatchScrollMSG`, which is the only place the event can be answered before the editor's own wheel handler sees it -- the dispatcher has no priority argument and the editor is registered first. |

**Enter must not start playtest.** Enter is also the editor's playtest shortcut, so
sending a message started the level — which read as the mod playing the game by itself.
Suppressing the key was not enough: swallowing `keyDown`, and then `keyUp` as well, both
left playtest starting, because the shortcut is not resolved through
`EditorUI::keyDown` in v6 (v6 added `CCKeyboardActionManager`, and many editor
shortcuts go through keybind handling rather than the layer's virtual).

The fix blocks the **action**: `EditorUI::onPlaytest` is swallowed for 750 ms after a
message is submitted, armed in `submitDraft` so it applies to every submit path. Pressing
Enter deliberately after that window still plays the level, which was verified.

The general lesson, since this cost three attempts: when a key does two things, find the
action and block that. Blocking the key only works if you own the path it travels.

**Enter in an empty box still starts playtest, and must release focus.** With no draft
there is nothing to submit, so the suppression window is never armed and the editor's
shortcut runs. That is the editor's behaviour, not the mod's, but focus stayed in the
text field, which made the level unplayable: every keystroke went to the chat box, and
typing during the attempt turned up in the panel afterwards. `ChatPanel::setPlaytestHidden(true)`
now calls `TextInput::defocus()`, so focus is released on the transition into playtest
however the playtest was started.

## 8.6.1 Playtest Auto-Hide

Hiding during playtest is driven by the editor's own actions rather than by a poll:
`EditorUI::onPlaytest` hides, `EditorUI::onStopPlaytest` and `EditorUI::playtestStopped`
restore. Those cover every way in and out of playtest (button, keybind, menu) and fire
before the first frame of the level is drawn. A per-frame `PlayLayer::get()` comparison is
kept as a fallback and logs when it fires; the flags are idempotent, so the two cannot
fight.

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

It carries the **name of the tool the turn is running**, from the `tool_call` event (`get_editor_state···`, `create_objects·`). Without it a forty-round turn looks exactly like a frozen one: the indicator is the only thing on screen for the whole turn, and a static word for thirty seconds tells the user nothing about whether work is happening. The event is sent *before* each call, not after -- after would only narrate history -- and a failed send is logged and ignored, because losing a label must never cost the turn.

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
| `max_tool_rounds` | Tool-loop rounds per turn | 40 |
| `max_objects_per_turn` | Objects the sidecar will request mutating per turn | 5000 |
| `turn_token_budget` | Total LLM tokens per turn | provider-dependent, required to be set |
| `turn_timeout_s` | Wall-clock cap per turn | 300 |
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

Since then: the mouse wheel scrolls the transcript, Ctrl+Shift+H hides the panel, the
panel hides itself for playtest, and **Revert** works (§8.4.1). Still not done, and
deliberately so: Markdown rendering (`MDTextArea`).

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
whole messages. **116 tests pass.**

A revert is reported to the model as a revert, not as a generic hand edit
(`agent.REVERTED_NOTE`). The distinction is not cosmetic: after a revert the model's own
transcript says the work succeeded, so "something changed" invites it to defend work
that no longer exists. One wrinkle worth knowing: a revert changes the object count, so
the mod's change detector sends `level_changed` right after `transaction_reverted`, and
the vaguer note must not be allowed to overwrite the sharper one.

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
