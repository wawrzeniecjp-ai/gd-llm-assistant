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
- Keys `155` and `156` track layer/order: `155,1` on the object with no `156`,
  `155,2,156,3` on the two with one, and `155,4` on the trigger.
- The trigger carries six extra keys (`36`, `7`, `8`, `9`, `10`, `35`), which is
  the first evidence that trigger properties are **just more keys** rather than a
  separate mechanism. One sample is not proof, but it is the direction the design
  assumed.
- `uniqueID` values observed: 14, 16, 17, 21 — non-contiguous, so IDs are not
  reused after deletion. Relevant to S7 and to D9's identity model.

### Reading the trigger's keys (inferred, not yet confirmed)

`36,1` is text on a **text trigger** and a colour id on a colour trigger, so at
least one key is overloaded by object type — a wrinkle the property schema must
handle. `7/8/9` = 255 are almost certainly R/G/B, and `10,0.5` an alpha or
brightness, because a freshly placed colour trigger takes the first colour
channel's default and those three channels are full-intensity. `35,1` is
unexplained and deliberately not guessed at.

**These readings are hypotheses.** The differential method above converts them to
facts; until then the schema must not rely on them.

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
