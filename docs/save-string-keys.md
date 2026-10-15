# Save-string key map (empirical)

Derived from live `GameObject::getSaveString()` output in the editor, not from
community documentation. Raw captures and the method are below; this file is the
reference for the property schema in `SPEC.md` §7.5.

## Why this file exists

The GD level format stores object properties as flat `key,value` pairs, where the
keys are small integers. That vocabulary is what any generic property mechanism
must speak. Deriving it from the game rather than from docs keeps it correct for
the exact GD build we target (2.2081), and makes re-derivation cheap after a game
update.

## Method

Read-only instrumentation, on the main thread:

1. Iterate `LevelEditorLayer::getAllObjects()` (**not** `m_objectVector`, which is
   a pre-sized table of nulls — see SPEC.md §6.1.6).
2. Log `object->getSaveString(editor)` once per distinct `object->m_objectID`.
3. Compare the same object's `getPositionY()` against the save-string `3` value to
   establish the coordinate relationship.

Differential refinement (not yet run): change exactly one property via its setter,
dump again, diff — the key that changed is that property's key.

## Captured samples (GD 2.2081, Geode 5.11.1)

Four object types from a test level. Both y values are shown because they differ
by a constant offset (below).

| Type | uniqueID | save string | Notes |
| --- | --- | --- | --- |
| 83 | 14 | `1,83,2,435,3,-105,155,1` | minimal: type, x, y, z_layer |
| 918 | 16 | `1,918,2,345,3,-15,155,2,156,3` | adds `156` |
| 1327 | 17 | `1,1327,2,375,3,42.5,155,2,156,3` | same shape as 918 |
| **899** | 21 | `1,899,2,165,3,195,155,4,36,1,7,255,8,255,9,255,10,0.5,35,1` | **colour trigger** |

### What these establish

- The vocabulary is **flat and shared**: `1`, `2`, `3` appear on all four types.
  Extra keys are **additive**, not a per-type schema.
- The trigger carries six extra keys (`36`, `7`, `8`, `9`, `10`, `35`), which is
  the first evidence that trigger properties are **just more keys** rather than a
  separate mechanism.
- `uniqueID` values observed: 14, 16, 17, 21 — non-contiguous, so IDs are not
  reused after deletion. Relevant to S7 and to D9's identity model.

### Trigger keys, resolved

Cross-checked against the modern key table (see "External reference" below). The
**names** from that table explain the **values** observed here exactly, which is
stronger evidence than either source alone:

| Key | Value here | Name | Reading |
| --- | --- | --- | --- |
| `155` | 4 | `color_1_index` | Integer; the object's first colour index |
| `36` | 1 | `interactible` | Bool |
| `7` / `8` / `9` | 255 | `red` / `green` / `blue` | Full intensity, as a fresh trigger would have |
| `10` | 0.5 | `duration` | Float; the trigger's fade duration |
| `35` | 1 | `opacity` | Float; 1.0 = fully opaque |

The non-trigger samples resolve too: `155,1` is `color_1_index` 1, and
`155,2,156,3` is `color_1_index` 2 with `color_2_index` 3. So `155`/`156` are a
**colour-index pair**, present on all objects.

### Correction history (recorded, not deleted)

This is the third attempt at `155`/`156`, and the path there is the point:

1. **First reading — z_layer/z_order. Wrong.** The values looked like a
   layer/order pair and that reading survived three consistent samples.
2. **Second reading — "undisclosed".** The older reference table doesn't list
   them, so they were written off as unknown. That was premature: the older table
   *did* say "suspected to be something related to optimizing colors", which was
   directionally right.
3. **Actual answer — `color_1_index` / `color_2_index`**, from the modern table.

Two lessons worth keeping. First, **a pattern that fits the data is not an
identified key** — step 1 failed that test and step 2 over-corrected by concluding
"unknowable" from one incomplete source. Second, when a key's meaning is unknown,
check *more than one* reference before declaring it opaque.

Also corrected: key `36` was earlier guessed to be an overloaded text/colour key.
It is `interactible`, a bool.

**Key meaning is now established for every key seen so far.** Nothing here
supports a per-type key overload, and a plain global key→name map is consistent
with all the data.

## Coordinate relationship (measured)

`getPositionY()` is the cocos position on the editor layer; save-string key `3` is
the level y. Across the four samples the difference is a constant **+90**:

| Type | save `3` | cocos y | difference |
| --- | --- | --- | --- |
| 83 | −105 | −15 | 90 |
| 918 | −15 | 75 | 90 |
| 1327 | 42.5 | 132.5 | 90 |
| 899 | 195 | 285 | 90 |

The earlier three-sample capture showed 89.5 for type 1327 and 90 for the others,
which was a logging-precision artefact (`{:.1f}` vs integer rounding); at one
decimal place the offset is exactly 90 for all four.

**This is why `ground_y` is not yet exposed in `get_editor_state`.** Treating
`getPositionY()` as the level y would displace every generated object by 90 units
— three grid blocks — silently, with no error to catch it. Whether 90 is universal
or derived from the ground layer or the editing layer is still unverified.

## External reference

The modern key table is
[FlowVix/gd-info-explorer](https://flowvix.github.io/gd-info-explorer/), with the
raw definitions at
[`src/lib/schema.txt`](https://raw.githubusercontent.com/FlowVix/gd-info-explorer/main/src/lib/schema.txt).
It is far more complete than the older
[Wyliemaster/gddocs](https://github.com/Wyliemaster/gddocs) table — which was
archived in January 2026 and states outright that 2.2 "adds a ton of new object
properties" that live elsewhere.

The names map onto `SPEC.md` §7.5's existing property list almost exactly, which is
the strongest argument for the hybrid schema — a name↔key dictionary plus a value
codec, rather than two parallel property systems:

| Spec property | Key | Type (per the table) |
| --- | --- | --- |
| `id` | `1` | Int |
| `x` / `y` | `2` / `3` | Float |
| `flip_x` / `flip_y` | `4` / `5` | Bool |
| `rotation` | `6` | Float |
| `editor_layer` | `20` | Int |
| `color_channel_1` / `color_channel_2` | `21` / `22` | Int |
| `z_layer` / `z_order` | `24` / `25` | Int |
| `scale` | `32` | Float |
| `scale_x` / `scale_y` | `128` / `129` | Float |
| `groups` | `57` | `GroupList` (values dot-separated) |

The table also supplies the value *encodings* §7.5 needs and doesn't yet define:
`$GroupList` is a period-separated list, `$HSVString` is
`<h>a<s>a<b>a<s_checked>a<b_checked>`, and text properties (`31`) are base64. It
also shows the type system is an **inheritance hierarchy** — a `ColorTrigger`
inherits `Trigger` inherits `Common` — so per-type property sets are real, even
though the key *numbers* stay globally consistent.

**Caveats.** It is crowdsourced and self-described as incomplete ("This data is
incomplete, help extend it"). So it is a strong naming aid and a good source of
value encodings, not an authority — which is exactly why the differential probe
exists: it verifies the specific properties the mod actually ships, in the GD build
we actually target, without depending on anyone's table being right.

## Verified mappings

Produced by `runDifferentialProbe()` (see "Method" above): create a scratch object,
dump, apply exactly one change, dump, diff. Each row is confirmed **in GD 2.2081**,
not quoted from a table.

| Property | Key | Change applied |
| --- | --- | --- |
| `x` | `2` | `setPosition` |
| `y` | `3` | `setPosition` |
| `rotation` | `6` | `setRotation` |
| `flip_x` | `4` | `setFlipX` |
| `flip_y` | `5` | `setFlipY` |
| `editor_layer` | `20` | `m_editorLayer` field |
| `color_channel_1` | `21` | `m_baseColor->m_colorID` |
| `z_layer` | `24` | `m_zLayer` field |
| `z_order` | `25` | `m_zOrder` field |
| `groups` | `57` | `addToGroup` |
| `scale_x` | `128` | `m_scaleX` **field** |
| `scale_y` | `129` | `m_scaleY` **field** |

`editor_layer`, `z_layer` and `z_order` respond to **direct field writes**, which
was worth confirming: the game could have derived them elsewhere, and a field write
is what `modify_objects` will need for them.

### The setters that silently do nothing

The most useful result in this table is what is *missing* from it:

| Attempted change | Result |
| --- | --- |
| `setScaleX(1.5f)` | **no change** |
| `setScale(1.5f)` | **no change** |
| `m_scaleX = 1.5f` | key `128` := 1.5 ✅ |

GD's serialiser reads the `m_scaleX`/`m_scaleY` **fields**, not the cocos node scale
that `setScale*` touches. So `setScaleX` compiles, is a real API, returns nothing,
and has no effect on the object's stored scale.

This matters directly for `SPEC.md` §7.5: a `modify_objects` implementation that
reached for `setScaleX` would report success while changing nothing, and nothing in
the code or the type system would indicate the problem. Scale must be written via
the fields.

The same shape of trap has now appeared three times in this project — the
unbindable `onExit`, the unpopulated `m_objectVector`, and now the ineffective
scale setter. The common feature is an API that exists and compiles but does not do
what its name implies, and in each case only a measurement caught it.

### Not yet verified

- `color_channel_2`. Writing `m_detailColor->m_colorID` produced no change, almost
  certainly because a standard block has no detail colour (the probe guards against
  a null `m_detailColor`). The table gives key `22` for `color_2`, but that is
  **unconfirmed** — it needs a probe on an object type that has a secondary colour.
- `opacity` (key `35`) is a trigger-only property, so a standard block correctly
  shows no change. Not a failure.

## Consequences for SPEC.md §7.5

Three corrections are required regardless of how that section is reworded:

1. **`m_objectVector` is the wrong enumeration source.** It reported 10000 entries,
   all null, on a level with a handful of objects. Use `getAllObjects()`.
2. **The string mechanism cannot modify.** `GameObject::getSaveString()` is
   read-only and there is no `setSaveString`. The string form covers reading and
   creation (`createObjectsFromString`, `addObjectFromVector`); modifications must
   go through setters or fields.
3. **Scale must be written via `m_scaleX`/`m_scaleY`, not `setScale*`.** The setters
   have no effect on the save string.

And one design conclusion, now backed by measurement rather than a table: the
verified keys line up one-to-one with the properties §7.5 already chose, and the
vocabulary is global and additive. So a **hybrid schema** — readable names for the
properties the LLM touches constantly, fronted over a generic numeric key map with
a small value codec — is the right shape. The two are the same dictionary seen from
two directions, not two competing systems.

Two corrections are required regardless of how that section is reworded:

1. **`m_objectVector` is the wrong enumeration source.** It reported 10000 entries,
   all null, on a level with a handful of objects. Use `getAllObjects()`.
2. **The string mechanism cannot modify.** `GameObject::getSaveString()` is
   read-only and there is no `setSaveString`. The string form is usable for
   reading and for creation (`createObjectsFromString`,
   `addObjectFromVector`); modifications must go through the per-property setters.

And one design input: the shared, additive vocabulary supports a **generic key
map**, optionally fronted by readable names for the properties the LLM touches
most. A fully generic map should wait for the differential derivation, since the
key overload at `36` shows that key meaning can depend on object type.
