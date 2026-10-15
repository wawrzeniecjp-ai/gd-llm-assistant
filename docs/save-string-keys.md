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

### Trigger keys, resolved against the reference

Cross-checked against the community key table (see "External reference" below).
This **corrected three earlier inferences**, which are recorded rather than
deleted, because the same over-reading is easy to repeat:

| Key | Value here | Reference says | Status |
| --- | --- | --- | --- |
| `7`, `8`, `9` | all 255 | Red, Green, Blue of a trigger's colour | ✅ matches the earlier guess |
| `10` | 0.5 | **Duration** of an effect in a trigger | ❌ earlier guess "alpha/brightness" was **wrong** |
| `35` | 1 | **Opacity** | ✅ consistent (1.0 = fully opaque) |
| `36` | 1 | **bool**, suspected "X position locked / unaffected by a Move trigger" | ❌ earlier "overloaded: text vs colour id" was **wrong** |
| `155` | 1, 2, 4 | not in the documented table; reference calls it *"suspected to be something related to optimizing colors, appears on all objects"* | ❌ earlier "z_layer" was **wrong** (z layer is key `24`) |
| `156` | 3 | same as `155` | ❌ earlier "z_order" was **wrong** (z order is key `25`) |

The `155`/`156` case is the instructive one. The observed values — `155,1` on the
first object, `155,2,156,3` on the two added later, `155,4` on the trigger — looked
like a layer/order pair, and that reading survived three consistent samples. It was
still wrong. A pattern that fits the data is not the same as an identified key.

**So: key meaning is still not established beyond `1`, `2`, `3`.** In particular,
nothing here supports a per-type key overload, and a plain global map is not
contradicted by the data.

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

The community key table is at
[Wyliemaster/gddocs → level-string](https://github.com/Wyliemaster/gddocs/blob/master/docs/resources/client/level-components/level-string.md).
Keys that matter to `SPEC.md` §7.5 map as follows:

| Spec property | Key |
| --- | --- |
| `id` (object type) | `1` |
| `x` | `2` |
| `y` | `3` |
| `flip_x` / `flip_y` | `4` / `5` |
| `rotation` | `6` |
| `editor_layer` | `20` (and `61` for Editor Layer 2) |
| `color_channel_1` / `color_channel_2` | `21` / `22` |
| `z_layer` | `24` |
| `z_order` | `25` |
| `groups` | `57` (integer array, values dot-separated) |
| `scale_x` | `128` (scale Y is not in the table) |

That the documented names line up one-to-one with the properties §7.5 already
chose is a strong argument for the hybrid schema: a name↔key dictionary plus a
value-codec (arrays, HSV, base64 strings) covers both the readable interface and
the generic escape hatch, without two parallel property systems.

**Two caveats about the reference itself.**

1. **It is stale and incomplete for our target.** The repository was archived in
   January 2026 with its last substantive commit two years earlier, and the page
   states plainly that 2.2 "adds a ton of new object properties" with the full set
   living elsewhere — it links
   [flowvix.github.io/gd-info-explorer](https://flowvix.github.io/gd-info-explorer/).
   We target GD 2.2081, i.e. 2.2, so the modern source is the one to use, and the
   table above should be treated as reliable for the classic keys only.
2. **It contains at least one error**: `128` is listed twice, both as "Scale X".
   Treat it as a strong naming aid, not an authority — which is why the samples
   above are captured from the game rather than quoted from it.

## Consequences for SPEC.md §7.5

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
