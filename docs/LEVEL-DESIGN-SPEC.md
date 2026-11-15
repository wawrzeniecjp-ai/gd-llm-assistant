# Level Design Layer — Specification

Status: **Draft 1 (2026-11-05)**. Companion to `SPEC.md`, which remains the authority
for the transport, the mod, transactions and the existing tools. This document
specifies everything needed to move the assistant from *editing objects* to
*designing levels*: gameplay that is checked rather than guessed, a representation
the model can reason in, music sync, a persistent plan, reusable patterns, style-aware
decoration, trigger effects, critique, and an evaluation harness.

Decisions marked **LOCKED** are architectural and should not be reopened during
implementation without a written amendment here. Decisions marked **DEFAULT** are
starting values or choices expected to be tuned. Anything that depends on how the game
actually behaves is tied to a **spike** (§20) and is not to be implemented from memory —
the project's history (`SPEC.md` §0.1b) is the reason.

---

## Contents

1. Goals, non-goals, and what "good" means
2. Principles
3. Architecture
4. Foundations: coordinates, object roles, level snapshot
5. GridText: a representation the model can read and write
6. Physics simulator and solver
7. In-game replay and calibration
8. Timeline and music
9. Level plan
10. Pattern library
11. Style profiles and decoration
12. Effects (trigger macros)
13. Schematic renderer
14. Critique and metrics
15. Evaluation harness
16. Knowledge base and prompt restructuring
17. Agent workflow and turn structure
18. Tool catalogue
19. Protocol additions
20. Spikes
21. Milestones
22. Risks and open questions

---

# 1. Goals, non-goals, and what "good" means

## 1.1 The problem

The current tools (`SPEC.md` §7.5) are editor primitives. They let a model place, move
and inspect objects reliably, but a model using them cannot tell whether a jump can be
made, whether timing feels fair, whether anything lands on the beat, or whether the
result looks coherent. It also has to reason in raw coordinates, which is the task
language models are worst at. Quality has nowhere to come from.

## 1.2 Goals

| ID | Goal |
| --- | --- |
| G1 | The assistant can **verify** that a section of gameplay is completable, and report how tight it is, before claiming it works. |
| G2 | The assistant reads and writes gameplay in a **compact text representation** rather than coordinate lists. |
| G3 | Gameplay can be **synced to music**: inputs and transitions placed against a beat grid. |
| G4 | A level can be built **across many turns** coherently, against a persistent plan. |
| G5 | The assistant can **decorate in the style of existing content** without breaking gameplay. |
| G6 | The assistant can add **common trigger effects** by intent, not by save-string key. |
| G7 | The assistant can **critique** a section against measurable criteria and fix what it finds. |
| G8 | Quality is **measured offline** by an evaluation suite that runs without the game. |

## 1.3 Non-goals (this spec)

- Fully autonomous one-prompt generation of a complete, publishable level. It is a
  composition of G1–G7 and falls out of them; it is not designed for directly.
- Platformer mode gameplay verification (deferred; §6.11).
- Uploading, rating, or downloading levels from the GD servers. Nothing here talks to
  RobTop's servers.
- Audio beat detection in v1 (optional later; §8.6).
- Replacing human taste. The assistant proposes; the user decides.

## 1.4 What "good" means, operationally

"Good" is not measurable directly, so it is replaced by proxies, each owned by a
component. A section is *acceptable* when all hard checks pass and *good* when the soft
scores are in the target band for its declared difficulty.

| Quality | Proxy | Hard/Soft | Owner |
| --- | --- | --- | --- |
| Completable | A route exists in the simulator | Hard | §6 |
| Fair | Minimum timing window ≥ threshold for the declared difficulty | Hard | §6.8, §14 |
| Readable | Hazards contrast with background; not occluded by deco; no blind landings | Hard (occlusion, blind) / Soft (contrast) | §14 |
| On beat | Fraction of inputs within tolerance of the beat grid | Soft | §8, §14 |
| Paced | Input density and intensity follow the plan's curve | Soft | §9, §14 |
| Coherent | Deco uses the section's style profile; palette consistent | Soft | §11, §14 |
| Not broken by deco | Gameplay object set unchanged by decoration passes | Hard | §11.6 |

---

# 2. Principles

These are **LOCKED**.

**P1 — Verify, don't assert.** Any claim about gameplay ("this is possible", "this is on
beat") must come from a tool result. The prompt forbids unverified claims, exactly as it
forbids invented object ids today.

**P2 — The model decides, deterministic code executes.** The model chooses structure,
patterns, styles, parameters and fixes. Deterministic sidecar code expands those choices
into objects (grid → objects, decorator → deco, macro → triggers). The model never places
hundreds of decoration objects one by one.

**P3 — Measured constants only.** Physics constants, hitboxes, grid offsets and speeds
come from the game, via calibration (§7) or a mod dump (§4.2), stored with provenance.
Values recalled from community knowledge are allowed only as *initial guesses* that
calibration overwrites, and are marked as such in the data file.

**P4 — Honest unknowns.** Where the simulator does not model something (an arbitrary
rotation, a slope before slopes exist, a trigger that moves a solid), the affected span is
reported as `unverifiable`, never silently treated as safe or unsafe.

**P5 — The mod stays thin.** New mod work is limited to things only the game process can
do: reading tables the game owns, the playtest, the music engine, and batch object
creation. All design logic lives in the sidecar (Python), where it is testable headless.

**P6 — Everything runs headless.** Every component except in-game replay and the mod's
own requests must run in tests and evals without Geometry Dash, against an in-memory
editor (§15.2).

**P7 — One undo step per section of work.** A build turn edits one section; its edits are
one transaction (`SPEC.md` §6.1.5). The user can always take back exactly one unit of work.

---

# 3. Architecture

## 3.1 Overview

```text
 Geometry Dash process                               Sidecar process
┌──────────────────────────────┐            ┌──────────────────────────────────────────────┐
│ Mod (C++)                     │            │ Agent loop (agent.py)                         │
│                               │            │   │                                           │
│  existing editor requests     │◄──────────►│   ├─ existing tools (tools.py)                │
│  + describe_object_types  §4.2│  ws v1     │   └─ design tools ──► design/ package         │
│  + get_level_objects      §4.3│            │                                               │
│  + create_objects_from_   §19 │            │  design/                                      │
│      strings                  │            │   level_model.py   snapshot + spatial index   │
│  + allocate_ids           §19 │            │   roles.py         object role table          │
│  + run_replay             §7  │            │   grid.py          GridText read/write        │
│  + get_level_music        §8  │            │   sim/             physics + solver           │
│  + level_revision counter §4.3│            │   timeline.py      x ⇄ time, beat grid        │
└──────────────────────────────┘            │   plan.py          persistent level plan      │
                                             │   patterns.py      pattern library            │
                                             │   style.py         style extraction           │
                                             │   decorate.py      decorators                 │
                                             │   effects.py       trigger macros             │
                                             │   render.py        schematic renderer         │
                                             │   critique.py      metrics + violations       │
                                             │   knowledge.py     knowledge base search      │
                                             │                                               │
                                             │  data/ roles.json  physics_constants.json     │
                                             │        patterns/   knowledge/                 │
                                             │  evals/ fake_editor.py  cases/  runner.py     │
                                             └──────────────────────────────────────────────┘
```

## 3.2 Decision log

| ID | Decision | Status |
| --- | --- | --- |
| LD1 | All design logic is a new Python package `gd_ai_assistant.design`, independent of the agent loop and of the transport. Tools in `tools.py` are thin adapters onto it. | LOCKED |
| LD2 | The design layer reads the level through a **per-turn Level Model snapshot** (§4.3), invalidated by any mutation or `level_changed`. Nothing about level content persists across turns. This keeps `SPEC.md` D4 (no mirror) intact. | LOCKED |
| LD3 | Object semantics (solid, hazard, orb, portal...) come from a **role table dumped from the game** (`GameObject::m_objectType` + hitbox), not from frame-name heuristics. Heuristics are a fallback only, and flagged. | LOCKED (S-L2 passed, 2026-11-07) |
| LD4 | The model's working representation for gameplay is **GridText** (§5): one character per 30-unit cell, with a fixed built-in legend and a per-call custom legend. | LOCKED |
| LD5 | Gameplay is verified by a **deterministic Python simulator** at the game's tick rate, with constants from calibration. In-game replay is ground truth for calibration and optional final checks, never the inner loop. | LOCKED |
| LD6 | Collision is **axis-aligned rectangles and circles**, with hitboxes rotated only in multiples of 90°. Anything else is `unverifiable` (P4). | LOCKED for v1 |
| LD7 | Time ⇄ position conversion is a **timeline** derived from the start speed and speed portals, using calibrated units-per-second. | LOCKED |
| LD8 | BPM and offset are **plan data**, supplied by the user or inferred by the model from what the user says; not detected from audio in v1. | DEFAULT |
| LD9 | The **level plan** is a versioned JSON document persisted on disk by the sidecar, keyed by the hello `level.key`, edited only through tools. | LOCKED |
| LD10 | Patterns are **GridText fragments + metadata**, verified by the simulator when added and when instantiated. | LOCKED |
| LD11 | Decoration is produced by **deterministic decorators driven by a style profile**. Decorators may only create objects whose role is `deco`, and every decoration call re-checks the gameplay invariant. | LOCKED |
| LD12 | Trigger effects are **macros compiled to save strings**, created through a new batch request `create_objects_from_strings`. Group/channel ids come from the game's own allocators. | LOCKED |
| LD13 | Images for the model come from two sources: the game (`capture_screenshot`) and a **sidecar schematic renderer** (Pillow) that works headless and can overlay the sim path, beats and sections. | LOCKED |
| LD14 | New mod requests are **additive to protocol v1**. A mod that does not know a request answers `UNSUPPORTED_REQUEST` (new error code); the sidecar then hides the dependent tools for the session. | LOCKED |
| LD15 | Domain knowledge lives in a **curated, searchable knowledge base** (`search_knowledge`), and the long-form tool guidance moves out of the system prompt into it. | LOCKED |
| LD16 | Builds proceed **one section per turn**. Long builds are a sequence of turns driven by the plan, optionally auto-continued (§17.3). | LOCKED |
| LD17 | Evaluation runs against an **in-memory fake editor** implementing the same `EditorLink` interface, so the full agent loop is exercised headless. | LOCKED |

## 3.3 Package layout

```text
sidecar/src/gd_ai_assistant/
  design/
    __init__.py
    coords.py          grid ⇄ level coordinate conversion, ground baseline
    roles.py           Role enum, RoleTable (load, lookup, fallback)
    level_model.py     LevelObject, LevelModel, SpatialIndex, snapshot loader
    grid.py            GridText parse/emit, legend, placement
    sim/
      __init__.py
      constants.py     PhysicsConstants (loaded from data/physics_constants.json)
      state.py         PlayerState, Mode, Speed
      collide.py       hitbox geometry, broad/narrow phase
      step.py          one tick per mode
      solver.py        route search, timing windows
      verify.py        public verify_section / simulate_inputs
    timeline.py        speed segments, x⇄ms, beat grid
    plan.py            LevelPlan model, persistence, patch ops
    patterns.py        Pattern model, library, instantiate, extract
    style.py           StyleProfile, extraction
    decorate.py        decorators, invariant check
    effects.py         macro definitions, compile to save strings
    render.py          schematic PNG
    critique.py        metrics, violation codes
    knowledge.py       KB loader, BM25 search
  data/
    roles.json                 (dumped from the game, §4.2)
    physics_constants.json     (from calibration, §7)
    patterns/seed/*.json
    knowledge/*.md
sidecar/evals/
  fake_editor.py
  cases/*.yaml
  fixtures/levels/*.json
  runner.py
  report.py
sidecar/scripts/
  dump_roles.py        drives describe_object_types, writes data/roles.json
  calibrate.py         drives run_replay probes, writes physics_constants.json
```

Dependencies added: `Pillow` (renderer), nothing else required. Optional extra
`[audio]` reserved for §8.6.

---

# 4. Foundations

## 4.1 Coordinates and the ground baseline

**LOCKED conventions.**

- **Level coordinates** are what the protocol already uses: `x` right, `y` up, GD units,
  object positions are object *centres*.
- **Cell size** `C = 30`.
- **Grid coordinates** `(col, row)` are integers. `col = floor(x / C)`.
  `row = floor((y - GROUND_TOP_Y) / C)`, so **row 0 is the first row of cells sitting on the
  ground**, row −1 would be inside the ground.
- A cell's centre is `x = col*C + C/2`, `y = GROUND_TOP_Y + row*C + C/2`.
- `GROUND_TOP_Y` is a **measured constant** stored in `data/physics_constants.json` as
  `ground_top_y`. The backlog records a +90 relationship (a ground-row block centre at
  y = 105) and the old demo constant matches it, but SPEC §0.1b also records a level where
  that constant placed objects under the ground. S-L1 must resolve this before GridText
  ships.
- **Found and fixed during M0 (measured 2026-11-07).** `docs/save-string-keys.md` measured
  `cocos y = save y + 90`, while `src/editor/properties.hpp` converted
  `level y = cocos y + 90`. `scripts/check_foundations.py` measured protocol `y` minus
  save key `3` over 28 objects: **+180 for every one**. The table was right and the mod's
  conversion had its sign flipped, so every `y` on the wire was six blocks above the level
  coordinate. That also explains §0.1b: protocol y = 105 was cocos 15, under the ground.
  Fixed in the mod (`cocosYToLevel = cocos − 90`); the camera (`get/set_camera`, capture
  viewports), which reported raw cocos y, now uses the same level y. Protocol `y` is now
  the save-string `y`, confirmed by `scripts/check_m0_fixes.py` (difference 0). That run
  also measured S-L1: a default block resting on the ground reads y = 15, so
  **`ground_top_y` = 0**, recorded as measured. It was one level; the three-level procedure
  below (including a raised start position) is still owed before M1 relies on it as a
  constant. `get_level_objects` keeps reporting positions as `query_objects` does
  (§4.3.2).

**Resolution procedure (S-L1).** In replay (§7), a cube standing still at the level start is
on the ground; its `y` minus half its hitbox height is the ground top. Measure it on three
levels: an empty one, the user's level from §0.1b, and one with a start position at a
non-default height. If the values agree, `ground_top_y` is a constant. If they do not, find
out why before going further. The likely suspects are a level setting or a start position
that moves the ground. In that case `ground_top_y` becomes a value the mod reports per
level (`get_level_settings.ground_top_y`), and `coords.py` takes it from the snapshot instead
of from the constants file. `coords.py` hides this choice from everything else, so either
outcome changes one function.

`lowest_y` remains in `list_object_types` and the prompt keeps it as a cross-check: if
`lowest_y` is far from `GROUND_TOP_Y + C/2`, the level has no ground-row content or the
baseline is wrong, and the tools say so.

## 4.2 The object role table

### 4.2.1 Why

Every component needs to know what an object *is* in gameplay terms: the GridText reader
must know a spike from a block, the simulator needs hitboxes, decorators must know what is
deco. The catalog's frame names are a vocabulary, not semantics.

### 4.2.2 Source (LOCKED)

A new mod request `describe_object_types { offset, limit }` iterates the ids in
`ObjectToolbox`'s table (the same source as `list_object_catalog`), and for each id:

1. Creates an instance **off-scene** with `GameObject::createWithKey(id)` — never added to
   the editor, never touching the level or undo stack — then calls `customSetup()`.
   `createWithKey` only builds the sprite; the type, hitbox size and radius are set by
   `customSetup`, which the editor and level loader call after creating an object. Without
   it every object describes as a solid 30x30 block (found in M0).
2. Reads `m_objectType` (the game's `GameObjectType` enum), the hitbox (`getObjectRect()`
   at the origin, plus `m_objectRadius` for circular hazards), whether it has a detail
   colour, and the default z-layer.
3. Releases it.

The result is paged like the catalog. `scripts/dump_roles.py` drives it once per game
version and writes `data/roles.json`, **committed**, with the game version and the dump
date. The sidecar loads the committed file; the live request exists so the file can be
regenerated, and so the mod can be asked about a single id the file does not know.

S-L2 must confirm that off-scene creation is safe for every id. Ids that crash or return
null are recorded in a denylist in the dump, which the mod's existing creation denylist also
uses. **Result (2026-11-07):** all 4093 catalog ids described with no crash and no refusal;
the denylist is empty. Spike 6x12, small spike 6x5.6, saw radius 32.3, orb 36x36, pad 25x4
and portals 34x86 match known values. The cross-check against the editor's hitbox view on
ten sampled ids has not been done.

The game types speed portals and the editor's camera/BPM guides as the generic `Modifier`,
so `roles.py` maps those few by id (`_MODIFIER_IDS`): speed portals to `portal_speed` with
their speed as the variant, guides to `deco`. Each override applies only while the game
still reports `Modifier` for that id.

### 4.2.3 Role model

`GameObjectType` values are mapped to a smaller **Role** enum the design layer uses. The
mapping table lives in `roles.py`. The raw `object_type` integer is kept so nothing is lost.

| Role | Meaning | Examples |
| --- | --- | --- |
| `solid` | Collidable block surface | blocks, platforms, slabs |
| `slope` | Collidable slope | slopes (sim: unverifiable until §6.11) |
| `hazard` | Kills on contact | spikes, saws, pits |
| `orb` | Activated by input while overlapping | yellow/pink/blue/green/red/black/dash/spider/teleport orbs |
| `pad` | Activated by contact | yellow/pink/blue/red/spider pads |
| `portal_mode` | Changes gamemode | cube, ship, ball, ufo, wave, robot, spider, swing |
| `portal_speed` | Changes speed | 0.5×, 1×, 2×, 3×, 4× |
| `portal_gravity` | Changes gravity | normal, inverse, toggle |
| `portal_size` | Mini / normal | |
| `portal_other` | Mirror, dual, teleport | sim: mirror and dual unverifiable in v1 |
| `trigger` | Effect trigger | move, color, pulse, alpha, toggle... |
| `start_pos` | Start position | |
| `collectible` | Coins, keys | |
| `deco` | No gameplay effect | everything with `GameObjectType::Decoration` |
| `special` | Anything else | |

Each entry in `roles.json`:

```json
{
  "id": 8,
  "object_type": 2,
  "role": "hazard",
  "subrole": "spike",
  "frame": "spike_01_001.png",
  "hitbox": { "shape": "rect", "w": 6.0, "h": 12.0, "dx": 0.0, "dy": 0.0 },
  "visual": { "w": 30.0, "h": 30.0 },
  "detail_color": false,
  "default_z_layer": "default",
  "variant": null
}
```

`subrole` and `variant` carry what the enum lumps together: an orb's colour, a speed portal's
speed, a gamemode portal's mode. The game's type enum separates most of these. Where it does
not (speed portals may share one type), the variant comes from a small curated table keyed by
id. Each entry in that table says where it came from (`"source": "frame:boost_02_001.png"`).
The hitbox numbers above are placeholders for shape only. Real values come from the dump.

### 4.2.4 Fallback

When an id is not in `roles.json` (an older dump, a new game version), `roles.py` derives a
role from the frame name (`spike` → hazard, `portal` → portal_other...). It marks the entry
`"source": "heuristic"`. The simulator then reports any span containing a heuristic-role
non-deco object as `unverifiable`.

## 4.3 The Level Model snapshot

### 4.3.1 Contract (LOCKED)

`LevelModel` is an immutable, in-memory view of the level for the duration of one tool
call sequence:

```python
@dataclass(frozen=True)
class LevelObject:
    uid: int                 # unique id (SPEC D9)
    type_id: int
    x: float; y: float
    rotation: float
    scale_x: float; scale_y: float
    flip_x: bool; flip_y: bool
    z_layer: int; z_order: int
    editor_layer: int
    groups: tuple[int, ...]
    color_1: int | None; color_2: int | None
    role: Role               # from RoleTable
    raw: Mapping[int, str]   # full save-string key → value, for triggers and anything else

class LevelModel:
    revision: int
    settings: LevelSettings          # from get_level_settings
    objects: Sequence[LevelObject]
    def in_rect(self, x1, y1, x2, y2, roles=None) -> list[LevelObject]: ...
    def by_uid(self, uid) -> LevelObject | None: ...
```

A uniform-grid spatial index (bucket = 1 cell column band of 10 columns) backs `in_rect`.

### 4.3.2 Loading

A new mod request `get_level_objects { cursor?, limit }` returns objects paged (default
1000, max 2000 per page). Each record is `{ object_id, id, properties, save }`:

- `properties` is the named property set, read by the **same code** as
  `get_object_properties` (position in protocol coordinates, rotation, scale, flip, layers,
  groups, colour channels). The sidecar never decodes these from the save string. The
  bindings do not annotate the rotation and flip keys (4–6), and the y offset is in doubt
  (§4.1), so decoding them would mean guessing.
- `save` is the full save string, for everything else (trigger parameters, hidden flags).
  The sidecar parses it into `LevelObject.raw` (key → text).

Each page carries `revision`. The cursor is `<offset>@<revision>`, and the mod refuses a
cursor from another revision with `LEVEL_CHANGING`. It also fails a page whose revision
moves while the page is being read (one object per job slice).

**Revision counter (as built).** The editor's undo and redo entry points are `win inline`
in the bindings and cannot be hooked, so the mod polls a fingerprint once per frame instead:
the object count, the sizes of the undo and redo lists, and the identity of the newest entry
on each. Any difference bumps the revision. The mod's own mutating jobs (`create_objects`,
`modify_objects`, `delete_objects`, `set_save_property`) bump it on every slice, since they
mostly bypass the undo list. Over-counting only costs a re-read. Under-counting is what
S-L10 checks for. If a change lands between pages, the sidecar discards the partial
snapshot and restarts, once. A second change raises `LevelChangingError`, which tools report
as "the user is editing".

### 4.3.3 Caching

The agent holds at most one snapshot. It is invalidated:

- after any mutating tool call in this turn (the snapshot is reloaded lazily on next use);
- when a `level_changed` event arrives;
- at the end of the turn.

A full reload of a 20k-object level is ~20 pages. That is acceptable once per mutation burst.
For large levels, the incremental option (§22, Q3) is deferred.

---

# 5. GridText

## 5.1 Purpose

GridText is the model's working language for gameplay. Language models handle small
character grids well and coordinate lists badly. A grid also shows structure at a glance
— gaps, staircases, corridors — in a way a list of numbers never does.

## 5.2 Format (LOCKED)

A GridText block is a header line followed by rows, **top row first**:

```text
@grid col=120 row=0 w=24 h=6
........................
........................
..............##........
.........o....##....^^..
....^.........##...####.
########################
```

- `col`, `row` give the **bottom-left** cell of the block in grid coordinates (§4.1). The
  last line written is row `row`, the line above it is `row+1`, and so on.
- `w` and `h` are required and must match the rows exactly. A mismatch is a parse error, not
  a guess.
- Every character is exactly one cell. Lines are ASCII only.
- Lines beginning with `;` after the header are comments and are ignored.

### 5.2.1 Built-in legend (LOCKED characters, DEFAULT ids)

The character set is fixed. Each character maps to a **role and orientation**, and the
object id is resolved by role through the active style (§11) or, failing that, through the
built-in default in `grid.py`. Every default id must exist in `roles.json` with the expected
role, and a test checks this.

| Char | Meaning | Orientation |
| --- | --- | --- |
| `.` | empty | — |
| `#` | solid block (full) | — |
| `=` | solid slab (half height, bottom of cell) | — |
| `-` | solid slab (half height, top of cell) | — |
| `^` `v` `<` `>` | spike pointing up / down / left / right | rotation 0 / 180 / 270 / 90 |
| `,` | small spike, pointing up, bottom of cell | — |
| `*` | saw / circular hazard | — |
| `o` `p` `b` `g` `r` `k` `d` | orbs: yellow, pink, blue (gravity), green, red, black, dash | — |
| `_` | yellow pad | follows gravity of the row's context |
| `~` | pink pad | |
| `!` | blue (gravity) pad | |
| `C` `S` `B` `U` `W` `R` `I` `G` | mode portals: cube, ship, ball, ufo, wave, robot, spider, swing | — |
| `1` `2` `3` `4` `5` | speed portals 0.5×, 1×, 2×, 3×, 4× | — |
| `+` `&` | gravity portal: inverse / normal | |
| `n` `N` | size portal: normal / mini | |
| `@` | start position | |
| `:` | **continuation**: this cell is occupied by a multi-cell object anchored elsewhere | read-only |
| `?` | **unrepresentable**: something is here that GridText cannot express; see `extras` | read-only |
| `0`, `6`-`9`, lowercase letters not listed above, `$ % ( ) [ ] { } \| / \\ ' "` | **custom legend** characters | per call |

`;` is not a cell character: it only starts a comment line, so a row can never begin with it.

**As built (M1).** Snap offsets are per character (`_SNAP_BY_CHAR` in `grid.py`), not
in `roles.json`: only the built-in legend needs them. Measured (S-L9): `,` -9, `_` and `~`
-13, `!` -12, and the editor puts a half slab at the **top** of its cell, so `-` is +8
(measured) and `=` is -8 (derived from the plank's hitbox); the rest 0 (the orb was
measured as the control). The editor's offsets are per object and do not follow one
geometric rule (SPEC.md 0.2b), so a custom legend entry may add `"align"`:
`top`/`bottom`/`left`/`right`, flush with that edge of its hitbox. `render_grid` reads an
object as aligned only on an exact match (+-0.5), otherwise `?`. The start position (id 31) is
typed by the game as a trigger-flagged `Modifier`, so `roles.py` maps it by id.

Orientation for the pad characters follows the gravity the solver reports at that cell.
Where there is no route yet, they face up. An explicit custom-legend entry overrides this.

Multi-cell objects (portals, large saws) are written as one anchor character at the cell
containing their centre. The reader fills the other covered cells with `:`. The writer
ignores `:` and errors if a `:` cell has no anchor next to it, which catches a common
off-by-one.

### 5.2.2 Custom legend

Any call may pass:

```json
"legend": {
  "a": { "id": 1338, "rotation": 90 },
  "x": { "id": 211, "scale_x": 0.5, "properties": { "color_channel_1": 4 } }
}
```

A custom entry can use any id the catalog knows, with any of the settable properties
(`SPEC.md` §7.5). Custom characters must come from the reserved custom set. Built-in
characters cannot be redefined, so `^` always means a spike.

### 5.2.3 Placement rules

- The object is placed at the cell centre **plus the per-id snap offset** recorded in
  `roles.json` (`snap: {dx, dy}`). That offset is what the editor's own grid placement applies.
  A half slab sits at the bottom of its cell, for example. S-L9 measures these offsets.
  Until then the offset is 0 and slabs and small spikes are marked `snap: "unmeasured"`.
- Rotation and flip come from the legend.
- `editor_layer`, `color` and `z` come from the custom legend, or else from the active style,
  or else from the object's defaults.

## 5.3 Reading: `render_grid`

```json
{ "x_range": [3600, 4320], "y_range": null, "layers": ["gameplay"], "max_cols": 200 }
```

- `y_range` null means rows 0 up to the highest gameplay object, plus 2 empty rows, capped at
  40 rows.
- `layers`: any of `gameplay` (solid, slope, hazard, orb, pad, portal_*, start_pos),
  `triggers`, `deco`. The default is `gameplay`.
- Limits: 200 columns × 40 rows per call. Wider requests error with the column count the
  model should split by.

Result:

```json
{
  "grid": "@grid col=120 row=0 w=24 h=6\n........\n...",
  "legend_used": { "#": { "id": 1, "count": 30 }, "^": { "id": 8, "count": 3 } },
  "extras": [
    { "uid": 4821, "id": 1743, "x": 3712.5, "y": 131.0, "rotation": 30,
      "reason": "rotation not a multiple of 90" }
  ],
  "overlaps": [ { "cell": [131, 1], "uids": [5010, 5011] } ],
  "notes": ["3 deco objects hidden (layers=gameplay)"]
}
```

Representation rules:

- An object is put on the grid only if it is within ±3 units of a cell centre (after its snap
  offset), its rotation is a multiple of 90°, and its scale is 1. Otherwise its cell shows `?`
  and the object is listed in `extras` with the reason. Nothing is silently snapped.
- When two gameplay objects share a cell, the cell shows the one with the higher-priority role
  (hazard > orb > pad > portal > solid) and the cell is listed in `overlaps`.
- `cols` / `rows` (inclusive pairs, in cells) may be given instead of `x_range` /
  `y_range`: after one read the model thinks in cells.
- When custom characters appear, the result also carries `legend_for_place_grid`, the
  legend that recreates them, so a copy through `place_grid` loses nothing.
- Ids without a built-in character get a custom character assigned automatically for this
  response, and `legend_used` reports it. That way the model can copy an unusual block type
  back into `place_grid`.

## 5.4 Writing: `place_grid`

```json
{
  "grid": "@grid col=120 row=0 w=24 h=6\n...",
  "legend": { },
  "mode": "add",
  "preview": false,
  "verify": { "gamemode": "cube", "speed": "1x", "entry_row": 0 }
}
```

- `mode: "add"` creates objects for non-empty cells and leaves everything else alone. It errors
  when a cell is already occupied by gameplay, listing the clashing cells.
  `mode: "replace"` first deletes all **gameplay** objects whose centre is inside the block's
  rectangle and leaves deco and triggers alone, then creates.
- `preview: true` changes nothing. It returns the object list it would create and, when
  `verify` is given, the verification result.
- `verify` is optional. When it is present the simulator runs over the block, plus 10 columns
  of context either side taken from the current level, and the result is attached. Placement is
  **not** blocked by a failed verification. The model sees it and decides, and the prompt
  requires it to either fix the problem or tell the user.
- Creation goes through the existing `create_objects` request (or `delete_objects` +
  `create_objects` for replace), inside the turn's transaction, and counts against the
  per-turn object cap.

Result:

```json
{ "created": 34, "deleted": 0, "created_uids": [ ... ],
  "verification": { "...": "see §6.9" } }
```

---

# 6. Physics simulator and solver

## 6.1 Scope by phase (LOCKED order)

| Phase | Modes | Features |
| --- | --- | --- |
| Sim-1 (M2) | cube | all five speeds, normal gravity, normal size, solids, hazards, yellow/pink orbs, yellow/pink pads, speed portals |
| Sim-2 (M5) | + ship, ball, ufo, wave | gravity portals and blue orb/pad, mini, all orbs except teleport/spider/dash |
| Sim-3 (M5) | + robot, spider, swing | spider orb/pad, dash orbs, green orb, black orb, red orb/pad |
| Later | — | slopes, dual, mirror (visual only, no physics impact — can be treated as no-op once confirmed), teleport, moving objects (triggers), platformer |

Anything outside the current phase that appears inside a verified span makes that span
`unverifiable` with the reason.

## 6.2 Time base

- Fixed step at **240 ticks per second** (GD 2.2's physics rate; S-L3 confirms). All
  routes, windows and traces are in ticks; milliseconds are derived (`tick * 1000/240`).
- Horizontal motion is constant per speed: `dx_per_tick = units_per_second[speed] / 240`.
  It is never integrated from forces.

## 6.3 Constants (LOCKED structure, measured values)

`data/physics_constants.json`:

```json
{
  "schema": 1,
  "game_version": "2.2091",
  "calibrated_at": "YYYY-MM-DD",
  "tick_rate": 240,
  "ground_top_y": { "value": null, "source": "S-L1 not yet run", "confidence": "guess" },
  "units_per_second": {
    "0.5x": { "value": 251.16, "source": "initial-guess", "confidence": "guess" },
    "1x":   { "value": 311.58, "source": "initial-guess", "confidence": "guess" },
    "2x":   { "value": 387.42, "source": "initial-guess", "confidence": "guess" },
    "3x":   { "value": 468.0,  "source": "initial-guess", "confidence": "guess" },
    "4x":   { "value": 576.0,  "source": "initial-guess", "confidence": "guess" }
  },
  "modes": {
    "cube": {
      "gravity": null, "jump_velocity": null, "terminal_velocity": null,
      "mini_scale": null,
      "orb_impulse":  { "yellow": null, "pink": null },
      "pad_impulse":  { "yellow": null, "pink": null },
      "hitbox": { "outer": [30, 30], "inner": null }
    }
  }
}
```

- Every leaf is `{value, source, confidence}` with `confidence ∈ {measured, derived, guess}`.
  The speed values above are the commonly cited ones and are kept only as starting points.
- The simulator **refuses to run** a mode if any of its required constants is `null`, and
  reports `"unverifiable": "constants not calibrated for <mode>"`. Guesses are allowed to
  run, but every result built on one carries `"constants": "uncalibrated"`. The prompt tells
  the model to mention this to the user.
- Per-speed differences in gravity or jump are modelled by keying constants by
  `(mode, speed)` where calibration shows they differ. The schema allows
  `"jump_velocity": {"1x": ..., "2x": ...}` as well as a scalar.

## 6.4 Player state

```python
@dataclass(frozen=True)
class PlayerState:
    tick: int
    x: float; y: float          # centre, level coordinates
    vy: float                   # units per tick
    mode: Mode
    speed: Speed
    gravity: int                # +1 normal, -1 flipped
    mini: bool
    grounded: bool              # on floor (or ceiling when flipped)
    holding: bool               # input currently held
    buffered: bool              # a press that has not yet been consumed by an orb (see S-L3)
    used_orbs: frozenset[int]   # uids of orbs/pads already activated (each fires once)
    dead: bool
```

## 6.5 The tick (LOCKED order)

Each tick, in this order (S-L3 checks the order against replay traces):

1. Apply input changes scheduled for this tick (`holding`, `buffered`).
2. Mode-specific vertical update (`vy` from gravity or hold, clamp to terminal velocity).
3. Advance `x += dx_per_tick`, `y += vy`.
4. Collect the objects overlapping the player's outer hitbox (broad phase: spatial index
   columns `[x - 60, x + 60]`).
5. Resolve **solids**: if the player was above the surface on the previous tick (below it
   when gravity is flipped), within a snap tolerance, land: set `y` to the surface, `vy = 0`,
   `grounded`. Otherwise, if the player's **inner** hitbox overlaps the solid, die.
6. **Hazards**: outer-hitbox overlap (circle–rect for circular hitboxes) → die.
7. **Pads**: overlap with an unused pad → apply its impulse or effect, mark it used.
8. **Orbs**: overlap with an unused orb and (`buffered`, or a press this tick) → apply the
   impulse or effect, consume the buffer, mark the orb used.
9. **Portals**: overlap → change mode, speed, gravity or size. Speed changes take effect on
   the next tick.
10. Cube only: if grounded and holding → jump (`vy = jump_velocity * gravity`).
11. Out-of-bounds (above the ceiling for the mode, below the ground in flipped modes) handled
    per mode.

The exact semantics of orb buffering, hold-to-jump on landing, and the snap tolerance are
**not** to be guessed. They are calibration targets (§7.4) with dedicated probe levels.

## 6.6 Collision geometry

- Hitboxes come from `roles.json`, scaled by `scale_x`/`scale_y` and rotated by
  multiples of 90° (swap w/h). Flip mirrors the hitbox offset.
- Shapes: `rect` and `circle`. Rect–rect is AABB overlap; circle–rect is closest-point
  distance.
- An object with rotation not a multiple of 90 inside the span → the span is
  `unverifiable` (LD6). (Many decorative rotated objects are role `deco` and therefore
  irrelevant; only gameplay roles count.)

## 6.7 Simulating fixed inputs: `simulate_inputs`

Deterministic replay of a given input script in the simulator, the same shape as the in-game
replay (§7.2), so the two can be compared directly:

```json
{ "start": { "x": 3600, "row": 0, "mode": "cube", "speed": "1x",
             "gravity": 1, "mini": false, "vy": 0 },
  "inputs": [ { "x": 3700, "action": "press" }, { "x": 3712, "action": "release" } ],
  "until_x": 4400 }
```

Inputs may be given by `x` (converted to the first tick at or after that x) or by `tick`.
The result is a trace (sampled every 4 ticks, plus every event), the outcome, and the death
location and cause object if any.

**As built (M2, `sim/inputs.py`).** `until_x` is required; `start.x` gives an explicit start.
An input by `x` is placed while the run plays (a jump snap or a speed portal moves later
ticks), and `inputs_by_x` reports the tick each one got; inputs the run never reached are
listed in `not_played`. Events are presses and releases, takeoffs, landings, orb and pad
impulses and speed changes. The trace keeps at most 400 rows: a longer run doubles the
stride (`sample_every` in the result) and keeps the ticks around every event.

## 6.8 The solver

### 6.8.1 Problem

Given a start state and an end `x`, find an input script that reaches the end alive, and
report how forgiving each input is.

### 6.8.2 Algorithm (LOCKED approach, DEFAULT parameters)

A **best-first search over decision points with state deduplication**:

- **Decision points** depend on mode:
  - cube/robot: only ticks where pressing changes the outcome — grounded, or overlapping
    an unused orb. Between decision points the state advances deterministically.
  - ball/spider/swing/ufo: a press is a discrete event; consider pressing at each tick
    where the player is grounded, can flip, or overlaps an orb, plus every
    `ufo_decision_stride` ticks (DEFAULT 6) for ufo.
  - ship/wave: hold/release; branch every `hold_decision_stride` ticks (DEFAULT 4).
- **Deduplication key**: `(tick, round(y, 0.5), round(vy, 0.05), mode, gravity, mini,
  holding, used_orbs_hash)`.
- **Heuristic**: `x` progress (all branches move at the same speed, so the search is
  effectively breadth-first by tick with a beam).
- **Beam width** DEFAULT 2000 states per tick, configurable. A search that would exceed
  the beam reports `"result": "inconclusive"`, never "impossible".
- **Prefer** routes with fewer inputs, then larger minimum window (§6.8.3) — applied as a
  final ranking among found routes, not inside the search.

Outcomes: `possible` (route found), `impossible` (search space exhausted, no route),
`inconclusive` (beam overflow or time budget hit), `unverifiable` (unsupported content;
the unverifiable spans are listed).

### 6.8.3 Timing windows

For each input in the chosen route, the **window** is the contiguous range of ticks
`[earliest, latest]` in which that input can occur, with all other inputs fixed, and the run
still succeeds. It is computed by re-simulating with the input shifted in both directions,
up to ±24 ticks (100 ms), and stopping at the first failure each way. The window is
reported in ticks and in milliseconds.

For hold inputs the window is computed separately for the press and the release.

A window of 1 tick (≈4 ms) is **frame-perfect** and is always a violation unless the plan
declares the section `extreme`.

### 6.8.4 Performance budget

- Cube, 1000 columns, typical density: ≤ 2 s in CPython on a desktop machine.
- Ship/wave, 300 columns: ≤ 5 s.
- `verify_section` has a hard wall-clock budget (DEFAULT 10 s) after which it returns
  `inconclusive` with progress (`reached_x`).

If the budget is not met at M2, the step and collision functions are the hot path and may be
vectorised or moved to a compiled extension. The public API does not change.

**Measured (2026-11-14, `scripts/bench_solver.py`).** The ten possible M2 sections solved
as one run from slot 1's start (997 columns, five speeds): 1.4 s, within the budget. It was
3.5 s before two changes: the solver looks the world up once per x and speed in a tick
rather than once per branch, and the cube's used orbs are a frozen set, so copying a branch
and keying it no longer rebuild it. Each M2 slot alone, solve and windows, now takes
30-390 ms (it was 150 ms-2 s).

## 6.9 Tool: `verify_section`

```json
{ "x_range": [3600, 4800],
  "start": { "mode": "cube", "speed": "1x", "gravity": 1, "mini": false,
             "row": 0, "vy": 0 },
  "start_from": "auto" }
```

- `start_from: "auto"` derives the start state by simulating from the nearest earlier
  checkpoint: the previous verified section end stored in the plan, or a start position,
  or the level start. `"explicit"` uses `start`.
- Result:

```json
{
  "result": "possible",
  "constants": "calibrated",
  "route": [ { "tick": 31, "x": 3638.9, "action": "press",
               "window_ticks": [28, 36], "window_ms": 33.3 } ],
  "exit": { "x": 4800, "y": 105, "vy": 0, "mode": "cube", "speed": "1x",
            "gravity": 1, "mini": false, "grounded": true },
  "difficulty": { "min_window_ms": 33.3, "p10_window_ms": 41.6,
                  "inputs_per_second": 2.4, "frame_perfect": 0 },
  "unverifiable": [],
  "deaths_without_input": [ { "x": 3660, "cause_uid": 5012 } ],
  "elapsed_ms": 412
}
```

`deaths_without_input` (where a no-input run dies) is cheap and tells the model where the
first obstacle is. For `impossible`, the result includes `furthest_x` and the cause objects at
the frontier, so the model knows where to look.

## 6.10 Determinism and tests

- The simulator is pure: same model + same start → same result, no randomness.
- Golden tests: every committed replay trace (§7.5) is re-simulated, and positions must match
  within tolerance (DEFAULT |Δy| ≤ 1 unit, |Δx| ≤ 0.5 unit at every sample, same death tick ±1).
- Property tests: a flat floor with no hazards is `possible` with zero inputs, at every
  speed and mode where that applies. A wall is `impossible`.

## 6.11 Deferred: slopes, platformer, moving objects

Slopes need a separate collision response and their own probes. Platformer mode makes
horizontal motion an input. Moving solids need the trigger timeline. All three get their own
amendment to this spec. Until then they are `unverifiable` (P4).

---

# 7. In-game replay and calibration

## 7.1 Purpose

Replay is the ground truth. It is used to (a) measure the constants, (b) record golden
traces for simulator tests, and (c) optionally confirm a solver route in the real game
before the model tells the user a section is finished.

## 7.2 Mod request: `run_replay`

```json
{
  "start": { "kind": "level_start" },
  "inputs": [ { "tick": 31, "action": "press", "player": 1 },
              { "tick": 40, "action": "release", "player": 1 } ],
  "max_ticks": 4800,
  "sample_every": 1,
  "time_scale": 1.0
}
```

`start.kind` is `level_start` or `start_pos` (with `uid` of an existing start position object).

Result:

```json
{
  "outcome": "death",
  "ticks": 812,
  "death": { "tick": 812, "x": 4123.4, "y": 135.0, "cause_uid": 5012 },
  "trace": [ { "tick": 0, "x": 0.0, "y": 105.0, "vy": 0.0, "mode": "cube",
               "speed": "1x", "gravity": 1, "mini": false, "grounded": true } ],
  "tick_rate_observed": 240
}
```

`trace` is returned as columnar arrays when `sample_every` is small, to stay under the frame
limit, or paged into multiple responses. S-L3 picks one of the two.

### 7.2.1 Implementation constraints (LOCKED)

- Entry and exit use the editor's own playtest (`EditorUI::onPlaytest` / `onStopPlaytest`),
  so the level and undo stack are untouched.
- Inputs are injected at **tick** granularity by hooking the game's fixed-step update and
  calling the game's own button handling (`GJBaseGameLayer::handleButton(down, button,
  isPlayer1)` or the equivalent queued-input path; S-L3 decides). Injection must happen
  inside the tick, not the frame, or timing will drift.
- Death is detected by hooking the player-destroyed path and recording the colliding object.
- Replay is **user-visible**: the editor is in playtest while it runs. The panel already
  auto-hides during playtest (`SPEC.md` §8.6.1). The mod shows a small, non-interactive
  "Assistant is testing…" label, and any user input aborts the replay with
  `CANCELLED`.
- `time_scale > 1` speeds the run up only if S-L4 shows the game's fixed step stays
  deterministic under a higher scheduler time scale. Otherwise the mod clamps to 1 and says
  so in the result.
- Replay is refused (`EDITOR_BUSY`) while a transaction is open with uncommitted work in
  progress. It runs between transactions, or the sidecar ends the transaction first.

**As built (S-L3, 2026-11-07).** The tick hook is `processCommands` (240/s, fixed
1/240 s); the move happens in `PlayerObject::update` just before it, so an input injected
at tick N moves the player from tick N+1. Positions are sampled with
`getRealPosition()` (`m_position` lags). Death stops the playtest from inside the
collision step and the game leaves `m_collidedObject` null for hazards, so the mod reports
the death tick and position and the **sidecar** names the cause from the geometry. The
trace is columnar (`trace.tick`, `trace.x`, ...), capped at 6000 samples per call. See
SPEC.md 0.2c.

### 7.2.2 Start positions

Testing from mid-level needs a start position. v1 uses only **existing** start position
objects (`start.kind = start_pos`). The sidecar may create one through the normal
transaction path, but only if the user agreed. It is a visible level edit, and removals go on
the user's undo stack (`SPEC.md` §6.1.5.1). S-L5 looks into a non-mutating alternative (setting
the editor's playtest start point directly).

**As built (2026-11-08).** The editor's playtest picks its own start position:
`LevelEditorLayer::findStartPosObject` (inline on Windows, so not hookable) takes the
enabled one with the highest target order, then the furthest right. A level with any start
position therefore never playtests from the level start. `run_replay` sets the
"disabled" flag on every other start position (on all of them for `level_start`) for the
instant the playtest starts, and restores the flags right after; nothing is saved in
between. The result echoes `start`. Whether a start position's first move comes after the
same 1-tick lead as the level start is not measured yet: `playtest_verify` reads it from
each run's trace and corrects itself (see `design/sim/playtest.py`).

**First run in the game (2026-11-09).** The start position lead is 1 tick, as from the
level start; no corrections fired. 9 of the 10 possible slots were confirmed. Slot 10 died
because the solver pressed on its window's last tick, so routes are now replayed with each
press in the middle of its window (`centre_route`). Gaps the run found, to be traced:
the jump differs by speed (higher at 2x, lower at 0.5x; calibrated at 1x only), the game
gets about one tick of x ahead on the stairs, and one tick before a window opens the
game often still survives.

The second run (the tricky-columns level, 0.5x) measured the 0.5x jump exactly: vy0 10.62
and gravity 0.212 (1x: 11.18, 0.216). The cube now takes both per speed
(`modes.cube.by_speed` in the constants, falling back to 1x), and calibration probe P9
measures 2x, 3x and 4x. With the 0.5x values the tricky columns need each press on one
exact tick. A start position starts at its own speed, 1x by default, so every M2 runway
now has a speed portal, and the assistant is told to put one after every start position.

The third run (slots 6-10 with edges, the tricky columns with edges; P9 measured 2x
11.42/0.215, 3x and 4x 11.23/0.216) confirmed all six, and the tricky columns' one-tick
windows exactly. Three more gaps were found and fixed: the game's x is a 32-bit float
(past x 16384 a 1x step is 1.298828, not 1.29825: half a unit ahead after 1000 ticks),
falling stops at vy -15 (terminal velocity), and a cube lands with its feet up to at least
8.72 below a block's top (landing tolerance 10.5, the middle of 8.72-12). With these, every
replayed tick and edge check of slots 7-10 and the tricky columns matches the simulator
(`tests/fixtures/replays/playtest_slot10.json`). One open: on slot 6's second stair the
game moved the cube one extra unit forward on landing (centre 1.4 past the block's right
edge, a spike next). No landing in P4 or elsewhere did that; it accounts for slot 6's two
remaining edge disagreements.

P10 and P11 (calibration, 2026-11-09) found the cause: jump snapping, the game's
`PlayerObject::checkSnapJumpToObject`. Jumping from one solid and landing on another
moves the cube towards the place on the new solid matching where it took off on the old
one, by at most 1 unit, but only for certain spacings between the two solids' centres.
P12 mapped it at every speed, with targets off the grid. It snaps only when the spacing
between the two blocks' centres falls in a narrow window, for a jump one block up or one
block down, never level:

| Speed | Up a block | Down a block | Step cap |
|---|---|---|---|
| 0.5x | 89.5-91.0 | 117.7-120.7 | 1 |
| 1x | 119.3-120.8 | 149.0-152.5 | 1 |
| 2x | 149.3-153.0 | 191.8-197.8 | 2 |
| 3x | 177.0-183.0 | 223.8-226.8 | 2 |
| 4x | none in 204-228 | none in 262-286 | - |

(Window edges are halfway to the nearest spacing that did not snap; the probe stepped 3
units.) The cube moves by the distance from where it lands to where it took off, mapped
onto the new block, plus one tick of x, capped by speed; every P12 snap fits that
exactly, including one under the cap (2x, spacing 150: 1.489). The simulator does the
same (`modes.cube.jump_snap`). With it, slots 6-10 match the game on every replayed tick
and every edge check (52 of 52), and all 407 P10-P12 runs are golden tests. Open: jumps
of two blocks up or down, and 4x outside the ranges tried.

Two more things P10/P11 showed. A shallow hit on a block's side from a start position is
reported on the same tick as the simulator or one tick later, never earlier, and
otherwise identical runs differ (P11 B, runs 29-45), so the golden tests allow one tick
there; the inner hitbox stays at half-width 4.5. And the simulator landed the cube on the
top of a stacked column's middle block (a top covered by the block above) and jumped on
from inside it, which made slot 12 possible for smaller inner hitboxes. Covered tops are
no longer floors.

P13 (calibration, 2026-11-10) measured yellow and pink orbs and pads at every speed, 200
runs. A yellow orb gives exactly the jump speed of that speed, and a pink orb 0.72 of it.
Pads do not depend on speed:

| Speed | Yellow orb | Pink orb | Yellow pad | Pink pad | Orb reach (ticks) |
|---|---|---|---|---|---|
| 0.5x | 10.62 | 7.646 | 16.0 | 10.4 | 65 |
| 1x | 11.18 | 8.05 | 16.0 | 10.4 | 53 |
| 2x | 11.42 | 8.222 | 16.0 | 10.4 | 43 |
| 3x | 11.23 | 8.086 | 16.0 | 10.4 | 36 |
| 4x | 11.23 | 8.086 | 16.0 | 10.4 | 30 |

(Orb reach is the number of press ticks that fired the orb on the probe's jump: the one
the simulator found staying longest in the orb. A jump that crosses it differently gives a
different count.)
The simulator reads these from `modes.cube.by_speed`. One more rule came out of P13: a
press buffered in the air still fires an orb that the cube first touches on the tick the
press is released. A one-tick press one tick before the first touch fired at every speed.
With that, every P13 run replays exactly as the game played it.

## 7.3 When the agent may replay

- **Calibration**: run by the developer via `scripts/calibrate.py`, not by the agent.
- **Final check** (`playtest_verify` tool): the agent may call it only when the plan's
  `allow_replay` is true (DEFAULT false) or the user asked in this turn. The tool's
  description says this, and the agent loop also enforces it.

## 7.4 Calibration procedure

`scripts/calibrate.py` connects as a normal sidecar, needs an open, **empty** editor
level, and:

1. Builds each probe level through the normal transaction path.
2. Runs the probe's input scripts through `run_replay` at each speed.
3. Fits the constants (closed-form where possible, e.g. gravity from a quadratic fit
   of `y(t)` after a jump; least squares otherwise).
4. Writes `physics_constants.json` and the traces to `sidecar/tests/fixtures/replays/`.
5. Undoes its own transactions, so the level ends up empty again.

Probe set for Sim-1 (cube):

| Probe | Measures |
| --- | --- |
| P1 flat floor, no input, 10 s per speed | `units_per_second`, `ground_top_y`, tick rate |
| P2 single press on flat floor | jump velocity, gravity, apex, airtime |
| P3 hold for 2 s | hold-to-rejump behaviour on landing |
| P4 block at increasing heights, press at fixed x | landing snap tolerance, inner hitbox from side deaths |
| P5 spikes at varying offsets | spike hitbox confirmation (vs `roles.json`) |
| P6 yellow/pink orb, press at offsets before/inside/after | orb hitbox, buffering, impulse |
| P7 yellow/pink pad | pad impulse |
| P8 speed portal mid-run | speed change timing |

Sim-2 and Sim-3 add their own probes when they start. A calibration run is reproducible: the
probes are code, and the output is diffable JSON.

## 7.5 Golden traces

Each probe trace is committed with its inputs and its probe level (as GridText + extras).
`test_sim_golden.py` re-runs each one in the simulator and checks it against the trace (§6.10).

---

# 8. Timeline and music

## 8.1 Timeline (LOCKED)

`Timeline` is built from the snapshot:

- The start speed comes from level settings. **Speed portals** are sorted by `x`, and each
  begins a segment `[x_i, x_{i+1})` at its speed.
- `ms_at(x)` sums `segment_length / units_per_second(speed) * 1000` across segments.
- `x_at(ms)` is the inverse.
- Mode portals do not affect horizontal speed and are ignored.
- The level's song offset (`m_songOffset`) is applied: `song_ms = level_ms + offset_ms`.
  S-L7 confirms the sign and units.

Timing granularity matches the simulator: a speed portal takes effect when the player
overlaps it, which is earlier than its centre by half the player's width plus half the
portal's. `timeline.py` uses the **activation x**, the same rule as §6.5 step 9, so the
simulator and the timeline never disagree.

## 8.2 Mod request: `get_level_music`

```json
{ "song_type": 0, "audio_track": 4, "custom_song_id": null,
  "song_offset_s": 0.0, "length_ms": 98000,
  "file_path": "C:/.../StereoMadness.mp3", "guidelines": [ { "ms": 1200.0, "color": 0.8 } ] }
```

- `length_ms` from `FMODAudioEngine::getMusicLengthMS` for the loaded level song (or from
  loading the file, S-L7).
- `file_path` is reported so a future audio extra (§8.6) can analyse the song. The sidecar
  only **reads** that file. It never writes it.
- `guidelines`: the editor's music guidelines, if the level has any (S-L6 locates their
  storage; the level string header is suspected). These are the user's own sync marks, and
  they are the most valuable timing data a level can carry.

## 8.3 Beat grid

Music data in the plan (§9):

```json
"music": { "bpm": 128.0, "offset_ms": 230.0, "beats_per_bar": 4,
           "source": "user", "tempo_changes": [ { "at_ms": 60000, "bpm": 140.0 } ] }
```

`beat_grid` tool:

```json
{ "x_range": [0, 6000], "subdivision": 2 }
```

Result:

```json
{ "beats": [ { "ms": 230.0, "x": 71.7, "col": 2, "bar": 1, "beat": 1, "sub": 0,
               "strength": "bar" } ],
  "units_per_beat_at_start": 146.0,
  "columns_per_beat_at_start": 4.87 }
```

`strength ∈ {bar, beat, sub}`. `columns_per_beat` is reported because a model designing on the
grid wants to know how many cells a beat spans at the current speed.

## 8.4 Guidelines as output

`set_guidelines { from_beat_grid: {x_range, subdivision}, colors: {bar, beat} }` writes the
editor's music guidelines, so the user **sees** the beat grid in the editor. That makes the
model's sync legible and editable. It is a level-settings edit with its own undo semantics
(S-L6). Until S-L6 resolves the storage, this tool is not built.

## 8.4.1 Sync placement helpers

- `place_grid` accepts `"snap_to_beats": {"tolerance_cols": 1}`. When it is present, the
  result lists every **input-requiring object** (orbs, and the cells where the solver route
  presses) and its offset from the nearest beat in ms. This is a report only. Objects are not
  moved automatically.
- `critique` reports beat alignment (§14).

## 8.5 BPM source (DEFAULT)

In order of preference: the user states it; it is already in the plan; the level has
guidelines with a regular spacing, from which the sidecar infers BPM by fitting (reported with
the fit error); the model asks. The model **never** guesses a BPM from the song's name.

## 8.6 Deferred: audio analysis

An optional `[audio]` extra may add onset and beat detection from `file_path`, and
structure segmentation (drop, build-up) from energy. It is out of scope for v1, but the plan
schema already has `music.sections` so its output has somewhere to go.

---

# 9. Level plan

## 9.1 Purpose

A level is built over many turns. The conversation history is token-trimmed and lost on
restart, so it cannot carry a design. The plan is the durable design record, and the thing
the model consults before every build step.

## 9.2 Storage (LOCKED)

- Path: `<sidecar data dir>/plans/<sha256(level.key)[:16]>.json`, where the data dir is a new
  setting `data_dir` (DEFAULT `%LOCALAPPDATA%/gd-ai-assistant` on Windows, the XDG data dir
  elsewhere).
- Written atomically (temp file + rename). Each write keeps the previous version as `.bak`.
- Never stored in the level file (README promise).
- `SPEC.md` §15 Q1 (level key collisions on rename) applies here too. The plan records
  `level_name` and `created_at`, and `get_level_plan` warns when the current level's name
  differs from the one recorded.

## 9.3 Schema (LOCKED v1)

```json
{
  "schema": 1,
  "level_key": "…", "level_name": "…", "created_at": "…", "updated_at": "…",
  "concept": "Short description of the level's idea and mood.",
  "target_difficulty": "hard",
  "music": { "bpm": null, "offset_ms": 0, "beats_per_bar": 4, "source": null,
             "tempo_changes": [], "sections": [] },
  "allow_replay": false,
  "styles": { "main": { "...": "StyleProfile, §11" } },
  "sections": [
    {
      "id": "s3",
      "name": "First ship",
      "start_ms": 21500, "end_ms": 32000,
      "x_range": [6700, 9960],
      "mode": "ship", "speed": "1x", "gravity": 1, "mini": false,
      "difficulty": 5,
      "intensity": 0.6,
      "style": "main",
      "status": { "gameplay": "verified", "deco": "todo", "effects": "todo" },
      "verified": { "revision": 1823, "min_window_ms": 41.6, "exit": { "...": "PlayerState" } },
      "notes": "Narrow corridor builds to the drop at 32 s."
    }
  ],
  "decisions": [ { "at": "…", "text": "User wants no dual sections." } ],
  "todo": [ "Ask about the ending." ]
}
```

- `x_range` is **derived** from `start_ms`/`end_ms` through the timeline whenever the plan is
  read, and stored only as a cache. If a speed portal moves, the sections follow the music.
- `difficulty` is 1–10 on the scale defined in the knowledge base (`kb:difficulty-scale`),
  which maps to target window and density bands (§14.3).
- `status.*` ∈ `todo | in_progress | built | verified | needs_fix`.
- `verified.revision` records the level revision at verification time. A section whose
  objects have changed since then is shown as `stale` by `get_level_plan`. The check is
  cheap: compare the uids and positions hash of the section's gameplay objects.

## 9.4 Tools

- `get_level_plan {}` → the plan, plus computed fields: derived `x_range`, staleness, and
  overall progress.
- `update_level_plan { ops: [...] }` with ops:
  `set_field {path, value}`, `add_section {section, after?}`, `update_section {id, fields}`,
  `remove_section {id}`, `add_decision {text}`, `set_music {...}`, `set_style {name, profile}`.
  Ops are validated against the schema as a whole. One invalid op rejects the batch.
- The plan is **not** part of the turn's undo. It is design metadata, not level content. The
  model is told so.

## 9.5 Prompt integration

When a plan exists, a compact rendering (≤ 600 tokens: concept, music, the section table with
status, the last 5 decisions) is added to the system prompt as a separate block under
"Level plan". The full plan is available through `get_level_plan`.

## 9.6 As built (M4, 2026-11-15)

- `design/timeline.py`: the timeline is built from the simulator's own portal bodies, and
  a speed change happens **on a tick**: the first tick whose move ends past the portal's
  activation x, a whole number of the old speed's steps into the segment, because the new
  speed applies from the next move. With that, the timeline matches the simulator to the
  tick at five portals (every speed); placing the change exactly at the activation x was
  off by just over a tick after a few portals. Calibration P14 checks the same course
  against the game (`scripts/calibrate.py P14`, fixture `cal_timeline`).
- The level's start speed now reaches the simulator: `get_level_settings` reports
  `start_speed` (read-only, from `LevelSettingsObject::m_startSpeed`), and verification,
  `simulate_inputs` and the route overlay start from it. Before, a level that starts at 2x
  was simulated from 1x.
- `get_level_music` reports the catalogue, track, name, offset and, for an official track,
  the file the game plays it from. `length_ms` and `guidelines` are null until S-L7 and
  S-L6; `set_guidelines` is not built.
- `design/music.py`: the beat grid in song time (first beat at `music.offset_ms`, tempo
  changes restart the spacing), and `alignment`: the fraction of route presses within
  35 ms of a half-beat (`beat_alignment`, 14.2).
- `design/plan.py`: storage as 9.2 under `data_dir` (new setting); ops as 9.4, validated
  against a JSON schema of 9.3 as a whole after all ops apply; derived `x_range` from song
  times; `verified` stamped by the sidecar with the revision and a hash of the section's
  gameplay objects when a section's gameplay is first marked verified, so a later change
  shows it `stale`; the prompt block is capped at 2400 characters.
- `place_grid` `snap_to_beats {subdivision}` reports each orb in the block and each press
  of its verified route with its offset from the nearest subdivision. Times come from the
  level before the block, so a block that places a speed portal says to check again.
- Evaluation: a case may bring a `[plan]` (seeded per run), and the `cube-on-beat` case is
  M4's beat-alignment check. Done-when status: the timeline matches the simulator at five
  portals; the game check (P14) and the 2-of-3-seeds eval are for the next live session.

---

# 10. Pattern library

## 10.1 Pattern format (LOCKED)

`patterns/<source>/<id>.json`:

```json
{
  "schema": 1,
  "id": "cube.triple-spike.1x",
  "name": "Triple spike",
  "tags": ["cube", "spike", "classic", "single-input"],
  "mode": "cube", "speed": "1x", "gravity": 1, "mini": false,
  "difficulty": 2,
  "grid": "@grid col=0 row=0 w=12 h=3\n............\n............\n.....^^^....",
  "legend": {},
  "floor": "#",
  "entry": { "row": 0, "grounded": true, "vy": 0 },
  "exit":  { "row": 0, "grounded": true },
  "verified": { "result": "possible", "min_window_ms": 33.3, "inputs": 1,
                "constants_version": "…" },
  "source": { "kind": "seed", "author": "…", "license": "…" }
}
```

- `grid` is relative (`col=0 row=0`). `floor` is the character placed under row 0 when the
  pattern is instantiated in a place that has no floor (`null` means no floor is added, for
  air patterns).
- `entry` and `exit` describe the player state the pattern expects and leaves. Chaining
  checks that one pattern's exit is compatible with the next one's entry.

## 10.2 Sources

| Source | Location | Who writes it |
| --- | --- | --- |
| `seed` | `sidecar/src/gd_ai_assistant/data/patterns/seed/` | us, committed, reviewed |
| `user` | `<data_dir>/patterns/` | `extract_pattern` from the user's own levels |

Patterns are **never** harvested from levels downloaded from servers (non-goal §1.3).

## 10.3 Verification rule (LOCKED)

- A pattern is saved only if the simulator returns `possible` at its declared mode and speed.
  For seed patterns this runs as a test. For user patterns it runs in `extract_pattern`.
- `verified.constants_version` records the constants file it was checked with. A constants
  update re-verifies every seed pattern in CI, and user patterns lazily when they are loaded.

## 10.4 Tools

- `search_patterns { query?, mode?, speed?, difficulty?: [min,max], tags?, limit? }` →
  ranked list: id, name, size, difficulty, window, and GridText preview.
- `place_pattern { id, col, row?, variant? }` where `variant ∈ {as_is, flip_gravity,
  rescale_speed:<speed>}`:
  - `flip_gravity` mirrors the grid vertically and maps orientation characters (`^`↔`v`).
  - `rescale_speed` scales columns by the ratio of units-per-second, rounds to whole cells,
    then **re-verifies**. If verification fails, it errors and nothing is placed.
  - Placement goes through `place_grid` (mode `add`) with `verify` filled in from the
    pattern, so the context columns either side are checked too.
- `extract_pattern { x_range, y_range?, name, tags, difficulty? }` → reads the region with
  `render_grid`, refuses if there are `extras` (unrepresentable content), derives mode, speed
  and entry state from the snapshot and solver, verifies, and saves to the user library.

## 10.5 Seed library target (M6)

At least 60 seed patterns: 25 cube, 10 ship, 8 ball, 6 ufo, 6 wave, 5 transitions (portal
in/out). Each speed-specific pattern is authored at 1×. The other speeds are generated
through `rescale_speed` and committed only if they verify.

---

# 11. Style profiles and decoration

## 11.1 Style profile (LOCKED schema v1)

Extracted from a region the user points at, by statistics over the snapshot:

```json
{
  "schema": 1,
  "source_region": { "x_range": [0, 3000] },
  "palette": {
    "channels": [ { "id": 1, "rgb": [20, 0, 60], "usage": "block_base" },
                  { "id": 2, "rgb": [255, 0, 180], "usage": "outline" } ],
    "background": [10, 0, 30], "ground": [20, 0, 60]
  },
  "solids": { "block_ids": [ { "id": 1, "share": 0.8 } ], "color_1": 1, "color_2": 2 },
  "deco_rules": [
    {
      "relation": "edge_top",
      "ids": [ { "id": 1717, "share": 0.6 }, { "id": 1718, "share": 0.4 } ],
      "density": 0.9,
      "z_layer": "t1", "color_1": 2,
      "rotation_jitter": 0, "scale": [1, 1]
    }
  ],
  "glow": { "enabled": true, "ids": [ ] },
  "background_fill": { "ids": [ ], "density_per_100_cells": 1.2, "z_layer": "b2" }
}
```

### 11.1.1 Relations (LOCKED)

Each deco object in the source region is classified by its relation to the nearest solid,
in order of precedence:

| Relation | Rule |
| --- | --- |
| `inside` | centre within a solid cell |
| `edge_top` / `edge_bottom` / `edge_left` / `edge_right` | centre within ½ cell of an *exposed* solid face of that side |
| `corner` | within ½ cell of an exposed convex corner |
| `on_hazard` | within ½ cell of a hazard |
| `near_solid` | within 3 cells of a solid |
| `free` | everything else |

`density` = the number of deco objects with that relation ÷ the number of candidate sites with
that relation (exposed faces, corners...) in the source region.

## 11.2 Extraction tool

`extract_style { x_range, y_range?, name }` → profile, saved into the plan's `styles` under
`name`. Its summary is returned for the model to describe to the user.

## 11.3 Decorators (LOCKED set v1)

Each decorator is a pure function `(model, region, profile, params, rng) → [ObjectSpec]`:

| Decorator | Places | Driven by |
| --- | --- | --- |
| `recolor_solids` | modifies existing solids' colour channels to the profile | `solids.color_*` |
| `edges` | objects on exposed faces and corners | `deco_rules` with `edge_*`/`corner` |
| `fill` | objects inside solid regions | `inside` rules |
| `hazard_accent` | objects around hazards | `on_hazard` rules |
| `near` | scatter near solids | `near_solid` rules |
| `background` | large background objects in empty space | `background_fill` |
| `glow` | glow objects along outlines | `glow` |

- `rng` is seeded (`seed` param, DEFAULT derived from the region), so the same call gives the
  same result and a re-roll is just a new seed.
- `density_scale` (DEFAULT 1.0) scales every rule's density.
- `recolor_solids` is the one decorator that modifies gameplay objects. It changes **only**
  colour channels, and the invariant (§11.6) allows exactly that.

## 11.4 Tool: `decorate`

```json
{ "x_range": [6700, 9960], "style": "main",
  "decorators": ["recolor_solids", "edges", "fill", "background"],
  "density_scale": 1.0, "seed": 17, "preview": false, "clear_existing_deco": false }
```

- `preview: true` → counts per decorator and a schematic image (§13). Nothing is changed.
- `clear_existing_deco: true` first deletes deco-role objects in the region. The model must
  have the user's agreement for this. The tool description says so, and the result reports
  how many were removed.
- Object caps apply (`SPEC.md` §6.1.4). A decorate call that would exceed the per-turn cap
  errors before creating anything and reports the count, so the model can lower density or
  split the region across turns.

## 11.5 Ids

Decorators use only ids that appear in the profile, and therefore in the user's own level.
The one exception is the `glow` and `background` defaults, which come from a curated table in
`decorate.py` checked against `roles.json`.

## 11.6 The gameplay invariant (LOCKED)

Before and after every `decorate` call, inside the same tool call:

1. Take the gameplay object set of the region: `(uid, type_id, x, y, rotation, scale, flip)`
   for every non-deco, non-trigger object.
2. After the decorator writes, re-read the snapshot. The set must be identical, apart from
   `color_*` changes made by `recolor_solids`.
3. Check every new object's role is `deco`.
4. If the section had a verified route, re-run `verify_section`. Deco cannot change physics,
   so this is a regression guard against role-table errors. A changed result is reported as
   an error and the tool's own creations are deleted.

---

# 12. Effects (trigger macros)

## 12.1 Mechanism (LOCKED)

A macro is a declarative recipe that compiles to **save strings** for one or more trigger
objects. Keys come from `properties.json` by name, never by literal number in the macro
table. Each macro has a fixture test: compiling a known input must reproduce a save string
recorded from a hand-made trigger in the real game (captured with `get_save_properties`).

The compiled strings are created through `create_objects_from_strings` (§19), inside the
turn's transaction.

## 12.2 Placement conventions (DEFAULT)

- A trigger fires when the player passes its `x` (not spawn or touch triggered, unless the
  macro says otherwise). Its `x` is the activation `x` of the requested time, from the timeline.
- `y`: triggers go in a **trigger lane** starting at row `plan.trigger_lane_row` (DEFAULT 20),
  stacked upwards when several share an `x` (one row per trigger), so they never overlap
  gameplay and are easy for the user to find.
- `editor_layer`: `plan.trigger_editor_layer` (DEFAULT: the current editor layer).

## 12.3 Ids

Groups, colour channels and item ids come from the game's own allocators
(`getNextFreeGroupID` etc., `SPEC.md` §6.1.6) through `allocate_ids { kind, count }`. The
sidecar never picks a free id by counting itself.

## 12.4 Macro set v1

| Macro | Params | Compiles to |
| --- | --- | --- |
| `color_change` | channel, rgb, at (ms or x or beat), duration_ms, blending? | 1 colour trigger |
| `color_pulse` | channel, rgb, at[], fade_in, hold, fade_out | 1 pulse trigger per `at` |
| `pulse_on_beats` | channel, rgb, x_range, every (beats), strength | pulse triggers on beat-grid positions |
| `move_group` | group, dx, dy (cells), at, duration_ms, easing, easing_rate | 1 move trigger |
| `move_loop` | group, dx, dy, period_beats, x_range, easing | alternating move triggers |
| `alpha_fade` | group, to_opacity, at, duration_ms | 1 alpha trigger |
| `toggle_group` | group, on/off, at | 1 toggle trigger |
| `shake` | strength, interval, at, duration_ms | 1 shake trigger |
| `assign_group` | uids, group? (allocated if absent) | modifies objects' `groups` (not a trigger) |

`at` accepts `{ "ms": … }`, `{ "x": … }`, or `{ "beat": [bar, beat, sub] }` (resolved through
the beat grid).

Gameplay-affecting triggers (`move_group` / `toggle_group` / `alpha_fade` on groups that
contain solids or hazards) mark the affected x-span `unverifiable` in the simulator (§6.11).
The tool result says so.

## 12.5 Tools

- `list_effects {}` → macro names, params, descriptions.
- `add_effect { macro, params, preview? }` → created trigger uids, the allocated ids, and the
  trigger-lane positions.
- `list_effects_in_range { x_range }` → triggers in the range, decoded through the macro
  table where they match a macro shape (otherwise described raw via `properties.json` names).

---

# 13. Schematic renderer

## 13.1 Purpose

Screenshots show what the player sees, which is often too busy to judge structure, and they
need the game. The schematic shows **structure**, works headless (evals, previews), and can
draw what no screenshot can: the route, the timing windows, the beats, the sections.

## 13.2 Tool: `render_schematic` (LOCKED behaviour)

```json
{ "x_range": [3600, 6000], "y_range": null,
  "layers": ["gameplay", "deco"],
  "overlays": ["grid", "route", "windows", "beats", "sections", "camera"],
  "px_per_cell": 12 }
```

- Output: a PNG written to the sidecar's temp directory, attached through the same mechanism
  as `capture_screenshot` (`SPEC.md` §7.5.6, images ride on a user message, queue drained).
  The size limit is checked by the same `capture.py` rules.
- Drawing: solids are filled rectangles in their channel colour (or role colour when
  `color_mode: "role"`), hazards red triangles or circles drawn as hitboxes, orbs and pads
  coloured circles, portals tall outlined bars labelled with their mode or speed, deco at 40%
  alpha, triggers as small ticks in a strip at the top.
- Overlays: `route` (the solver trace as a polyline, presses as dots), `windows` (bars under
  each press, width = window), `beats` (vertical lines: bars solid, beats dashed), `sections`
  (plan sections shaded and labelled), `camera` (the player's visible rectangle at selected
  presses, for blind-jump review).
- **As built (M1):** only the `grid` overlay draws; the others return a note naming the
  milestone that brings them. A hazard is drawn as its sprite outline (faint) with its
  hitbox solid inside, because a spike's deadly area is a fraction of its art.
- Max image size 1600×600 px. Wider ranges reduce `px_per_cell` automatically, with a floor
  of 4. Below that the call errors and asks for a narrower range.

---

# 14. Critique and metrics

## 14.1 Tool: `critique`

```json
{ "x_range": [6700, 9960], "section_id": "s3", "checks": "all", "image": true }
```

Runs, against the snapshot and the plan section (if any): verification, the metrics below,
and an optional schematic with violations marked. Result:

```json
{
  "section": "s3",
  "verdict": "needs_fix",
  "metrics": { "playable": "possible", "min_window_ms": 12.5, "p10_window_ms": 29.1,
               "inputs_per_second": 3.1, "beat_alignment": 0.62,
               "hazard_contrast_min": 1.4, "occluded_hazards": 2, "blind_inputs": 0,
               "empty_stretches": 0 },
  "targets": { "min_window_ms": [25, null], "inputs_per_second": [1.5, 3.5],
               "beat_alignment": [0.75, null] },
  "violations": [
    { "code": "TIGHT_WINDOW", "severity": "error", "x": 7710, "uids": [5120],
      "detail": "Press at x=7710 has a 12.5 ms window; difficulty 5 requires ≥ 25 ms." }
  ]
}
```

`verdict ∈ {ok, needs_fix, unverifiable}`: `needs_fix` if any `error` violation.

## 14.2 Metrics and violation codes (LOCKED definitions, DEFAULT thresholds)

| Code | Severity | Definition |
| --- | --- | --- |
| `IMPOSSIBLE` | error | solver result `impossible` |
| `UNVERIFIABLE` | warn | any unverifiable span (listed) |
| `FRAME_PERFECT` | error | any window ≤ 1 tick |
| `TIGHT_WINDOW` | error | window below the difficulty band minimum |
| `TOO_DENSE` / `TOO_SPARSE` | warn | inputs/s outside the difficulty band |
| `OFF_BEAT` | warn | a route press more than `beat_tolerance_ms` (DEFAULT 35) from the nearest beat subdivision, when the plan has music |
| `LOW_ALIGNMENT` | warn | beat alignment fraction below band |
| `OCCLUDED_HAZARD` | error | > 30% of a hazard's hitbox area covered by deco on a higher z with opacity ≥ 0.5 |
| `LOW_CONTRAST` | warn | contrast ratio between a hazard's colour and the background colour at its x below 2.0 (WCAG relative luminance formula; static colours only in v1, colour triggers ignored and reported) |
| `BLIND_INPUT` | error | the landing point of a press (where the player is next grounded or next presses) is outside the camera's view at press time |
| `INVISIBLE_GAMEPLAY` | error | a gameplay object with opacity 0, or on a hidden group at that x |
| `EMPTY_STRETCH` | warn | > 2 s with no inputs and no deco (only when the section's deco status is `built`) |
| `STYLE_DRIFT` | info | solid ids or palette outside the section's style profile |
| `MISSING_TRANSITION` | warn | mode/speed in the plan section differs from the simulated state at section start |

Camera model for `BLIND_INPUT` (DEFAULT): the player sits at a fixed screen x fraction, and the
visible width at gameplay zoom comes from calibration (`camera.player_screen_x`,
`camera.visible_width_units`), measured in replay by S-L3. Until they are measured this check
reports `UNVERIFIABLE`.

## 14.3 Difficulty bands

`kb:difficulty-scale` defines, for difficulty 1–10, the target `min_window_ms`,
`p10_window_ms`, and `inputs_per_second` ranges per mode. The initial values are authored by
us and marked provisional. They are tuned against levels the user rates in the eval suite
(§15.5). The table is data (`data/difficulty_bands.json`), not code.

---

# 15. Evaluation harness

## 15.1 Purpose

Without measurement, every prompt and tool change is a guess. The harness runs the real
agent loop on fixed tasks, headless, and scores the results with the same metrics as §14.

## 15.2 Fake editor (LOCKED)

`evals/fake_editor.py` implements `EditorLink` (`agent.py`) in memory:

- Supports every request the agent uses: state, query, summarize, create/modify/delete,
  selection, transactions (with revert), colour channels, level settings,
  `get_level_objects`, `describe_object_types` (from `roles.json`),
  `create_objects_from_strings`, `allocate_ids`, `get_level_music` (from the fixture),
  `get_camera`/`set_camera` (a simple model).
- `capture_screenshot` is answered with a schematic render (§13). The result says it is a
  schematic, so a model is not misled about what it is looking at.
- `run_replay` is answered by the simulator, and the result says so.
- Validates every request and response against `protocol/v1` schemas, so the fake cannot
  drift from the protocol.
- Save strings are produced by the fake with the same encoder as `effects.py`, round-tripped
  through `save_properties.py`.

The fake editor also replaces ad-hoc fakes in `test_tool_loop.py` over time.

## 15.3 Level fixtures

`evals/fixtures/levels/<name>.json`: `{ settings, objects: [save strings], music, plan? }`.
Fixtures come from the user's own levels via a dev script that calls `get_level_objects` and
`get_level_settings`, or are built synthetically by test code. They are committed only with
the level author's consent.

## 15.4 Cases

`evals/cases/<id>.yaml`:

```yaml
id: cube-section-from-empty
fixture: empty-1x
plan: plans/empty-128bpm.json
prompt: >
  Build the first 15 seconds as an easy cube section on the beat.
budget: { max_tool_rounds: 40, max_turns: 1 }
assert:
  hard:
    - playable: possible
    - violations_max: { severity: error, count: 0 }
  soft:
    - beat_alignment: { min: 0.75 }
    - inputs_per_second: { between: [1.0, 2.5] }
    - objects_created: { between: [20, 400] }
judge: false
```

Initial suite (M3): 12 cases across cube building, fixing an impossible section, adding a
portal transition, extending a pattern, recolouring, decorating (once M8 lands), effects
(once M9 lands). Each milestone adds cases for what it enables.

## 15.5 Runner and report

- `uv run python -m evals.runner --provider anthropic --model … --cases all --seeds 3`
- Every case is run `seeds` times (provider temperature as configured). Scores are
  pass rates per assertion and medians per metric.
- The report is JSON plus a static HTML page with schematics per run, written to
  `evals/out/<timestamp>/`. `evals/out` is gitignored.
- Cost guard: `--max-cost-usd`, estimated from token usage, stops the run when exceeded.
- `--provider scripted` runs the harness in CI with recorded transcripts, to test the
  harness itself, not the model.

**As built (M3, 2026-11-15).** Differences from the above:

- Cases are TOML (`evals/cases/<id>.toml`, read with the standard library), not YAML.
  A case has `prompt` or `prompts` (one per turn), an optional `section = [x0, x1]` that
  `playable` and the difficulty metrics are measured over, and `assert.hard` /
  `assert.soft` tables. A plain value must be equal; `{min}`, `{max}` or `{between}` bound
  a number; `created_roles.<role>` and `tools_used.<tool>` reach into counts. A metric the
  run could not measure fails as `n/a`. Every case that asserts `playable` also asserts
  `edited = true`, because an empty level is already playable.
- Metrics now: status, rounds, tokens, time, tool calls and errors, screenshots, objects
  created / deleted / moved and created roles, `verified_after_last_edit` (R4: a
  `verify_section`, `playtest_verify` or `place_grid` with `verify` at or after the last
  edit), colours set, and from verifying the section: `playable`, `min_window_ms`,
  `inputs_per_second`, `frame_perfect`. Beat alignment and violations wait for M4 and M7.
- Fixtures are JSON objects as `{id, x, y, ...named properties}`, written by
  `scripts/make_eval_fixtures.py`: an empty level, an impossible wall, a repeating
  pattern, M2 slots 2 and 12 moved to the level start, and the tricky columns.
- The fake editor answers `capture_screenshot` with a schematic of the camera's view; the
  tool result says it is a schematic, not the game's graphics.
- `--replay <folder>` plays recorded turns instead of a model (`evals/recordings/`, also
  written by `--record-to`); the test suite replays two of them, which is the CI check of
  the harness. There is no cost guard in dollars, since the harness does not know the
  provider's prices: `--max-total-tokens` stops starting new runs past a token count.
- Each run writes `transcript.json` and `section.png`; the report is `report.json`,
  `report.html` and `summary.md` (scores only, the form `evals/BASELINE.md` takes).

## 15.6 LLM judge (optional, DEFAULT off)

A case may set `judge: true`. A separate model call scores the final schematic against a
rubric from `kb:judge-rubric` (1–5 per criterion, with reasons). Judge scores are reported
**separately** from hard metrics and never gate a pass.

---

# 16. Knowledge base and prompt restructuring

## 16.1 Knowledge base (LOCKED format)

`data/knowledge/<id>.md`:

```markdown
---
id: difficulty-scale
title: Difficulty scale and timing bands
tags: [difficulty, timing, design]
sources:
  - "RobTop, GD Editor guide (robtopgames.com/files/GDEditor.pdf), section …"
  - "Authored from calibration data, 2026-11"
---
Body in our own words. Summaries, not copies.
```

- `search_knowledge { query, tags?, limit? }` → ranked chunks (BM25 over heading-split
  chunks, implemented in `knowledge.py`, no embeddings dependency), each with `id`, `title`,
  chunk text, and `sources`.
- `read_knowledge { id }` → the full entry.
- Content is authored, never bulk-copied (BACKLOG #11). Each entry cites its sources.

Initial entries (M10): `difficulty-scale`, `gamemode-basics`, `readability`, `sync-basics`,
`section-structure` (intro, build, drop, outro), `transitions`, `decoration-layers`,
`colour-and-contrast`, `triggers-overview`, `judge-rubric`, plus the tool guides below.

## 16.2 Prompt restructuring (LOCKED)

The system prompt is currently ~4k tokens of tool guidance. It is split as follows:

- **Core prompt** (≤ 2.5k tokens): role, hard rules (P1, no invented ids, no false claims,
  untrusted content), coordinates and GridText summary, the build workflow (§17), and
  "consult `search_knowledge` for details".
- **Tool guides** move to the KB as `guide-*` entries (`guide-colours`, `guide-save-properties`,
  `guide-camera-and-capture`, `guide-level-settings`). Tool descriptions keep the one-line
  essentials.
- **Plan block** appended when a plan exists (§9.5).

The existing test that ties the prompt to `tools.py` constants is kept and extended to the
GridText legend.

---

# 17. Agent workflow and turn structure

## 17.1 The build workflow (prompted, LOCKED shape)

1. **Concept and plan.** On a build request with no plan, the model agrees a concept, music
   (BPM, offset) and a section list with the user, writes them to the plan, and stops. It
   builds nothing in this turn unless the user asked it to proceed directly.
2. **Per section, one turn each** (LD16):
   1. `get_level_plan`, `beat_grid` for the section.
   2. Gameplay: `search_patterns` / `place_pattern` and `place_grid`, with `verify`.
   3. `verify_section`. Fix until `possible` and inside the difficulty band, or stop and
      report why.
   4. Mark the section `gameplay: verified` in the plan, with the exit state.
3. **Decoration pass** per section (separate turns): `extract_style` (once), `decorate`
   with preview, then apply, then `critique`.
4. **Effects pass**: `add_effect` against the beat grid, then `critique`.
5. **Review**: `critique` across the whole level. Fix errors and report warnings.

The user can enter at any step ("decorate this part like the start", "fix the ship section",
"make this harder"). Each step works without the earlier ones, falling back to sensible
defaults: with no plan, `verify_section` infers mode and speed from the snapshot.

## 17.2 Turn limits

- Build turns are bounded by the existing `max_tool_rounds` (40) and `turn_timeout_s`
  (300). Local tools (sim, grid, render) do not count against `request_timeout_s`. They have
  their own budgets (§6.8.4).
- The per-turn object cap (`max_objects_per_turn`, DEFAULT 5000) is unchanged. `decorate`
  and `place_grid` count their creations against it.

## 17.3 Auto-continue (DEFAULT off)

Sidecar setting `auto_continue_sections` (DEFAULT 0). When it is N > 0 and a turn ends with
the plan showing a next `todo` section in the same pass, the sidecar starts a new turn by
itself with a synthetic user message (`[auto] continue with section sN`), up to N times. Each
auto turn is its own transaction and its own undo step. Any user message or panel interaction
stops the chain. The panel shows the chain state, which needs a small protocol addition:
`turn_started.auto: true`.

## 17.4 Progress reporting

Every design tool reports progress the same way existing tools do (`_report_progress`). Long
local tools (solver, decorate) emit intermediate progress (`verify_section: 40% (x=5200)`)
through the existing `progress` event (`SPEC.md` §7.6).

---

# 18. Tool catalogue

Existing tools are unchanged unless listed. "Local" = sidecar only. "Mod" = needs a new mod
request.

| Tool | Kind | Mutates | Milestone | Section |
| --- | --- | --- | --- | --- |
| `render_grid` | local (snapshot) | no | M1 | §5.3 |
| `place_grid` | local → existing create/delete | yes | M1 | §5.4 |
| `render_schematic` | local | no | M1 | §13 |
| `verify_section` | local | no | M2 | §6.9 |
| `simulate_inputs` | local | no | M2 | §6.7 |
| `playtest_verify` | mod (`run_replay`) | no (playtest) | M2 | §7.3 |
| `get_level_plan` | local | no (plan only) | M4 | §9.4 |
| `update_level_plan` | local | plan only | M4 | §9.4 |
| `beat_grid` | local (+ `get_level_music`) | no | M4 | §8.3 |
| `get_level_music` | mod | no | M4 | §8.2 |
| `set_guidelines` | mod (S-L6) | level settings | M4 | §8.4 |
| `search_patterns` | local | no | M6 | §10.4 |
| `place_pattern` | local → `place_grid` | yes | M6 | §10.4 |
| `extract_pattern` | local | user library only | M6 | §10.4 |
| `critique` | local | no | M7 | §14 |
| `extract_style` | local | plan only | M8 | §11.2 |
| `decorate` | local → create/modify/delete | yes | M8 | §11.4 |
| `list_effects` | local | no | M9 | §12.5 |
| `add_effect` | local → `create_objects_from_strings`, `allocate_ids` | yes | M9 | §12.5 |
| `list_effects_in_range` | local | no | M9 | §12.5 |
| `search_knowledge` | local | no | M10 | §16 |
| `read_knowledge` | local | no | M10 | §16 |

Tool-count management: the provider receives all tools whose dependencies are available
(LD14). Tool descriptions stay short, and the details live in the KB.

All new mutating tools set `mutates_objects=True` in `tools.Tool` and implement
`object_count` from their arguments where possible. Where the count is only known after
expansion (`place_grid`, `decorate`, `add_effect`), the tool expands first, then checks the
cap against the expanded count **before** sending any mutation. This preserves the existing
"refuse before mutating" rule (`tools.Tool.object_count` docstring).

---

# 19. Protocol additions

All additive to v1 (LD14). Each gets a schema in `protocol/v1/editor.schema.json` and a
registry entry in `envelope.schema.json`.

| Request | Payload | Result | Section |
| --- | --- | --- | --- |
| `describe_object_types` | `{ offset?, limit? (≤500) }` | `{ entries: [RoleEntry], total, next_offset, denylisted: [id] }` | §4.2 |
| `get_level_objects` | `{ cursor?, limit? (≤2000) }` | `{ objects: [ { object_id, id, properties, save } ], revision, next_cursor, total }` | §4.3 |
| `create_objects_from_strings` | `{ objects: [ { save: string } ] }` (≤1000) | `{ created: [uid], count }` | §12 |
| `allocate_ids` | `{ kind: "group"\|"color"\|"item"\|"editor_layer", count (≤64), exclude?: [int] }` | `{ ids: [int] }` | §12.3 |
| `run_replay` | §7.2 | §7.2 | §7 |
| `get_level_music` | `{}` | §8.2 | §8 |
| `set_guidelines` | `{ guidelines: [ { ms, color } ] }` | `{ count }` | §8.4 |

Changes to existing messages:

- `get_editor_state` result: add `revision` (integer).
- `level_changed` event: add `revision`.
- `turn_started` event: add optional `auto` (boolean) (§17.3).
- Error codes (§7.7): add `UNSUPPORTED_REQUEST` (the mod does not implement this request
  type; replaces the `INVALID_REQUEST` the mod used to send for unknown types) and
  `LEVEL_CHANGING` (a `get_level_objects` page or cursor straddled an edit).

`create_objects_from_strings` rules (mod side):

- Each string is parsed by the game (`createObjectsFromString` per S-L8, or per-object
  creation from a parsed string). Validation happens before anything is created, and one bad
  string rejects the whole batch: a type id (key `1`) on the mod's creation denylist →
  `INVALID_OBJECT_ID`; position keys `2`/`3` missing or outside the level bounds →
  `INVALID_REQUEST`. The mod never rewrites a string to make it pass.
- Undo grouping follows the same transaction rules as `create_objects`. S-L8 confirms
  `createObjectsFromString` can be made to honour them. If not, the mod parses the save
  string and creates each object with `createObject` followed by the same property
  application `set_save_property` uses.

---

# 20. Spikes

Each spike has an exit criterion and blocks the listed milestone. Findings are recorded in
`SPEC.md` in the style of §7.5.x (measured, with what failed).

| ID | Question | Exit criterion | Blocks |
| --- | --- | --- | --- |
| S-L1 | Is the ground top a constant in level coordinates? If not, where does it come from? | `ground_top_y` measured identically (±0.5) on 3 levels, or a per-level source found and reported by the mod | M1 |
| S-L2 | Is off-scene `GameObject::createWithKey` safe for every id, and do `m_objectType`, `getObjectRect`, `m_objectRadius` give the hitbox? | Full dump without crash; spike/block/orb hitboxes match the editor's hitbox view on 10 sampled ids | M0 |
| S-L3 | Playtest control: tick hook, input injection at tick granularity, death hook, trace sampling; tick rate; orb buffering semantics | A scripted single jump reproduces the same trace (±0.01 units) across 5 runs; death object reported | M2 |
| S-L4 | Does raising the scheduler time scale keep physics identical? | Trace at 4× time scale equals trace at 1× | M2 (optional) |
| S-L5 | Can the playtest start point be set without creating a start position? | Yes/no with mechanism | M2 (optional) |
| S-L6 | Where are music guidelines stored, and can they be written with undo? | Read and write round-trip on a real level | M4 (`set_guidelines` only) |
| S-L7 | Song length, file path, offset sign/units for official and custom songs | Values for 2 official + 1 custom song match the editor | M4 |
| S-L8 | `createObjectsFromString` inside a transaction: undo behaviour, object limit, uid reporting | 100 triggers created and reverted with one undo | M9 |
| S-L9 | Per-id grid snap offsets (slabs, small spikes, orbs) as applied by the editor's own placement | Offsets for the built-in legend ids recorded in `roles.json` | M1 (for `=`/`-`/`,` only) |
| S-L10 | Revision counter: does every mutation path (user edit, undo, redo, paste, mod) increment it? | Counter changes in each of the 6 cases | M0 |

---

# 21. Milestones

Each milestone ends with the tests listed, the evals listed passing at the stated rate, and a
`SPEC.md` changelog entry. They are ordered by dependency. M10 can run in parallel from M1
onwards.

## M0 — Foundations

- Mod: `describe_object_types`, `get_level_objects`, revision counter, `UNSUPPORTED_REQUEST`.
- Sidecar: `design/` package skeleton, `roles.py` + `scripts/dump_roles.py` + committed
  `roles.json`, `level_model.py` with spatial index and caching, `coords.py`.
- `evals/fake_editor.py` with existing requests + the two new ones, schema-validated.
- Tool hiding on `UNSUPPORTED_REQUEST` (LD14).
- **Done when:** snapshot of a 10k-object level loads in < 3 s; role lookups cover 100% of
  ids in the user's test level; fake editor passes the existing tool-loop tests.

## M1 — GridText and schematic

- `grid.py` (parse, emit, legend, placement), `render_grid`, `place_grid` (without `verify`),
  `render_schematic` (without route/windows overlays).
- Prompt: GridText section.
- **Done when:** round-trip property test (`emit(parse(g)) == g` for any valid grid; read →
  write → read stable on fixture levels); live test: the model rebuilds a hand-made 30-column
  section from its own `render_grid` output with no diff.

## M2 — Cube simulator, replay, calibration

- Mod: `run_replay`. Sidecar: `sim/` Sim-1 scope, `scripts/calibrate.py`, golden traces,
  `verify_section`, `simulate_inputs`, `playtest_verify`, `place_grid.verify`, schematic
  `route`/`windows` overlays.
- **Done when:** all golden traces match (§6.10); 20 hand-made cube sections (10 possible,
  10 impossible, built by the user) classified correctly by the solver; perf budget met.

## M3 — Evaluation harness v1

- `evals/` runner, report, 12 cases, scripted-provider CI job.
- **Done when:** a baseline report exists for the current supported model, and it is
  checked in as `evals/BASELINE.md` (scores only).

## M4 — Timeline, music, plan

- `timeline.py`, `get_level_music`, `beat_grid`, `plan.py` + tools + prompt block,
  `set_guidelines` (if S-L6 resolved), `snap_to_beats` report.
- **Done when:** timeline matches replay-measured `(x, ms)` at 5 speed portals within 1 tick;
  eval "cube section on the beat" passes beat alignment ≥ 0.75 in 2/3 seeds.

## M5 — Full simulator

- Sim-2 and Sim-3 scopes, their probes and golden traces, difficulty bands data.
- **Done when:** golden traces for every mode match; mode-specific evals added and baseline
  recorded.

## M6 — Patterns

- `patterns.py`, tools, 60 seed patterns, CI re-verification.
- **Done when:** every seed pattern verifies at each committed speed; eval "build a 20 s
  section from patterns" passes hard checks in 3/3 seeds.

## M7 — Critique

- `critique.py`, all codes in §14.2 (camera-dependent ones gated on calibration), schematic
  violation markers.
- **Done when:** each violation code has a fixture that triggers it and one that does not;
  eval "fix this section" (fixture with 4 seeded violations) clears all errors in 2/3 seeds.

## M8 — Style and decoration

- `style.py`, `decorate.py`, tools, invariant.
- **Done when:** the invariant holds across 1000 randomised decorate runs on fixtures
  (property test); eval "decorate like the first section" produces `STYLE_DRIFT`-free output
  and no new errors.

## M9 — Effects

- Mod: `create_objects_from_strings`, `allocate_ids`. Sidecar: `effects.py` macros with
  save-string fixtures, tools.
- **Done when:** every macro's compiled string matches its in-game fixture; a
  `pulse_on_beats` over 16 bars creates and reverts with one undo.

## M10 — Knowledge base and prompt split (parallel track)

- `knowledge.py`, tools, initial entries, prompt restructuring.
- **Done when:** core prompt ≤ 2.5k tokens; evals show no regression versus the M3 baseline.

---

# 22. Risks and open questions

| # | Risk / question | Mitigation / decision owner |
| --- | --- | --- |
| R1 | Simulator diverges from the game in edge cases (orb buffering, landing snap), producing confident wrong verdicts | Golden traces; `playtest_verify` for final checks; any new mismatch becomes a probe + trace |
| R2 | Python solver too slow for long ship/wave sections | Budgets and `inconclusive` (never a false "impossible"); hot path can move to a compiled extension without API change |
| R3 | Role table wrong for some ids (game enum lumps things) | Curated `variant` table with provenance; heuristic roles make spans unverifiable |
| R4 | Model ignores verification results and claims success | Prompt rule P1; agent loop adds a system note when a turn ends with a section touched but no `verify_section` call since the last mutation in it; eval assertion |
| R5 | Plan and level drift (user edits by hand) | Revision-based staleness (§9.3); hand-edit note already exists |
| R6 | Decoration density overwhelms object caps or performance | Caps checked before mutation; `density_scale`; split across turns |
| R7 | Trigger macros produce subtly wrong save strings | Fixture per macro captured from the game (§12.1) |
| Q1 | Level key stability across renames (inherits `SPEC.md` §15 Q1) | Owner decision; until then the plan warns on name mismatch |
| Q2 | Should `playtest_verify` run automatically at the end of a build turn? | DEFAULT no (user-visible, takes real time); revisit after M2 usage |
| Q3 | Incremental snapshots for very large levels (50k+ objects) | Deferred; only needed if full reloads exceed 5 s in practice |
| Q4 | Whether to add a second, cheaper model for sub-tasks (pattern search, critique summaries) | Deferred until eval data shows where the tokens go |
