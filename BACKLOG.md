# Backlog: deferred from 2026-10-31 session

Items the user asked to log rather than do now.

## UI / panel (C++)

1. **Input row lets clicks through.** The `TextInput` node does not claim its own
   touches, so clicking in the empty part of the input row hits whatever is behind the
   panel (editor buttons). Fix direction: claim the input row's rect in
   `ChatPanel::ccTouchBegan` (currently only `messageAreaRect()` is claimed), or give
   `m_inputRow` its own touch delegate.
2. **Panel blocks clicks while hidden.** `setVisible(false)` stops drawing but the
   touch delegate stays registered, so the invisible panel still eats clicks. Fix:
   unregister/re-register with the touch dispatcher on visibility change
   (`applyVisibility()` is the one place to hook), or have `ccTouchBegan` return false
   when `!isVisible()` / `m_hidden` / `m_playtestHidden`.
   - NB: the same bug class means the collapsed panel may also over-claim; check the
     header strip vs `messageAreaRect` while collapsed.

3. **Tool-call feedback in the transcript.** While a turn runs, the panel shows
   "Thinking..." for the whole turn. It should instead show what is happening:
   "Called list_object_types", "Created 5 objects", etc.
   Protocol already has what is needed: `progress` event exists (§7.6) and
   `turn_finished.actions` carries per-action summaries. Simplest route: have the
   sidecar emit a `system_message` (or a new `tool_call` event) as each tool starts,
   and have the panel replace the thinking line with that text.

## Reasoning content

4. **Model reasoning is not shown.** DeepSeek returns `reasoning_content` for
   reasoning models; the adapters drop it. Options: surface it as a collapsed
   "Thinking" block in the transcript (streamed), or keep it out of the UI and only log
   it. Needs a protocol addition if it is to reach the mod, and a sanitiser pass since
   it is untrusted text.

## From GDBuilderAI comparison (worth adopting)

5. **Property table generated from the bindings.** They generate
   `properties.json` (save-string key -> meaning, per object class) from
   `GeometryDash.bro` via `gen-properties.mjs`. We have the same `.bro` in
   `build/_deps/bindings-src/bindings/<ver>/`. This is the path to trigger support
   without guessing keys.
6. **Richer object catalog fields.** Their `objects.json` columns:
   `id, frame, tab, type, class, w, h, zLayer, zOrder` — dumped from the running game.
   Ours has `id, frame, grid_size`. Adding `class` (GameObject / EffectGameObject /
   RingObject / ...), `tab` (build tab) and real `w`/`h` would let the model tell a
   ring from a spike from a trigger without guessing from the frame name.
7. **`gd_search_properties` tool.** Look up a property by name or save-string key.
8. **`gd_fill_area`** (fill/outline a rect) and **`gd_transform_objects`** using the
   editor's own routines so object IDs stay valid.
9. **`gd_set_camera`** — move/zoom the editor view; also "follow the work" so placed
   objects come into view.
10. Their coordinate convention differs from ours and may be worth checking:
    "positions at object centres, top of the ground at y = 0; a block on the ground in
    the first column is at (15, 15)". Ours uses level Y with a measured +90 offset.
