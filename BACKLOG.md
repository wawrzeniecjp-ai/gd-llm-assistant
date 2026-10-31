# Backlog: deferred from 2026-10-31 session

Items the user asked to log rather than do now.

## UI / panel (C++)

0. **Chat input does not wrap.** `TextInput` is single-line, so as text grows
   the field appears to shrink its font (it is scrolling, not wrapping) and a
   line break cannot be entered at all. Long messages become unreadable while
   being typed. `TextInput` has no multi-line mode, so this needs either a custom
   node or a wrapped preview above the field.

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

## Knowledge base

11. **Reference material for the agent.** Sources the user identified:
    - `https://www.robtopgames.com/files/GDEditor.pdf` -- RobTop's official editor manual
    - `https://www.gdcreatorschool.com/` -- community site, much of it video

    Worth turning the text-shaped parts into a queryable base rather than prompt
    text: a `search_knowledge` tool over curated notes (object conventions,
    gamemode/speed spacing tables, design patterns, what "sync" means in
    practice). Same shape as `search_properties`. Summarise and cite, do not
    bulk-copy -- and note the video content is not usable directly, so anything in
    the base has to be authored from what we can actually read.

## Music and sync

12. **Build around the music.** The most requested quality signal in GD levels, and
    the measurable part is more tractable than it looks:

    - `FMODAudioEngine::getMusicTimeMS(channel)` and `getMusicLengthMS(channel)`
      exist, so the mod can sample the song position.
    - Sampling `(playerX, musicTimeMS)` **during a playtest** measures units-per-ms
      directly instead of trusting a remembered constant. Sampled per speed segment
      it stays exact across speed portals.
    - `GJGameLevel::m_songID` / `m_audioTrack` and `LevelSettingsObject::m_songOffset`
      give the song identity and offset.
    - **BPM is stored nowhere** -- it is an audio property, not level data. It has
      to come from the user, from a curated table per song id, or from decoding the
      audio (out of scope).

    MVP: `get_level_music` (song, length, offset, measured units-per-ms) plus a beat
    grid the model can place against. The harder half is *structure* -- build-up,
    drop, phrase boundaries -- which no API provides and would have to come from the
    model's own musical judgement plus markers the user sets.
