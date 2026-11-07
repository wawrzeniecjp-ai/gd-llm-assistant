# M0 in-game checks

Everything in milestone M0 that runs without the game is built and tested (221 sidecar
tests, and the mod builds). What's left needs Geometry Dash running. It's one sitting of
about 10–15 minutes, in two scripts.

Both scripts *are* the sidecar for as long as they run: same port, same token, same
handshake. So **close the `start-sidecar.bat` window first**, or they will compete for the
port.

## Before you start

1. The new mod build is already installed: `cmake --build build` installs the `.geode` into
   your mods folder. If you rebuilt elsewhere since, reinstall it.
2. Launch GD **through Steam** and open a level in the editor. A level you don't mind
   editing, with a few hundred objects or more, makes the timing check meaningful. The
   checks add, move and delete a block or two, and undo covers all of it.

## 1. Foundations check: S-L10, coordinates, timing

From `sidecar/`:

```bash
uv run python scripts/check_foundations.py
```

It connects when the editor is open, then:

| Step | What it checks | What you do |
| --- | --- | --- |
| 1 | The new mod build is loaded (it reports `revision`) | nothing |
| 2 | Off-scene description of a block and a spike (a quick S-L2) | nothing. If the game crashes here, see section 3 |
| 3 | Protocol `x`/`y` vs save-string keys 2/3 | nothing. Prints the difference |
| 4 | Full snapshot time | nothing |
| 5 | Revision moves on each kind of edit (S-L10) | follow the prompts in the console: place, move, rotate, edit, duplicate, delete, undo, redo, and two "change nothing" controls |

It finishes with a create/modify/delete of its own through a transaction, then writes
`build/foundations-report.json`. **Send me that file**, or paste the console output.

What I'll do with the results:

- **Step 3** decides the y-offset question in LEVEL-DESIGN-SPEC §4.1. A difference of 0
  means `properties.hpp` is right and the old table is wrong. A difference of 180 means
  the conversion has the wrong sign, which is a real bug in the current mod, not only the
  design layer.
- **Step 5:** any step marked `UNEXPECTED` is an edit path the fingerprint misses, which
  needs another signal before snapshots can be trusted.
- **Step 4:** M0's target is under 3 s per 10k objects.

**Done 2026-11-07.** It found three bugs: y off by +180, a spike described as a solid
block, and Edit Object not moving the revision. All three are fixed in the mod (SPEC.md
0.2a). Step 4's timing needs a bigger level to mean anything.

## 1b. Confirm the fixes, and measure the ground (S-L1)

**Restart the game first**, so it loads the rebuilt mod. Then, from `sidecar/`:

```bash
uv run python scripts/check_m0_fixes.py
```

It checks the object descriptions and the y fix with no input from you. Then it asks you to:

1. Place one plain default block on the lowest row, resting on the ground, and leave only
   it selected. This measures the ground height.
2. Change its colour through Edit Object. The revision should move.
3. Deselect it, then select it again. The revision should *not* move.
4. Scroll so the block is in the middle of the screen. This checks the camera's y.

It writes `build/m0-fixes-report.json`. Send me that file.

**Done 2026-11-07.** Every check passed; the ground top is y = 0.

## 2. Role dump: S-L2, in full

Run this **after 1b**: before the `customSetup` fix, every object came out as a solid block.

```bash
uv run python scripts/dump_roles.py
```

This instantiates every object type off-scene, a few thousand of them. Nothing is added to
the level. It writes `sidecar/src/gd_ai_assistant/data/roles.json` and prints counts by
role and a few spot checks. That file is meant to be committed.

**Done 2026-11-07.** 4093 ids, no crash, none refused. Speed portals and the editor guides were relabelled from `special` by id (SPEC.md 0.2a).

## 3. If the game crashes during the dump

The mod logs `describe_object_types: id N` **before** creating each object, so the last
such line in the newest file under `<Geometry Dash>\geode\logs\` names the culprit. Add
that id to `kDescribeDenylist` in `src/exec/design_jobs.cpp` (with a comment), rebuild, and
rerun. Or just send me the id and I'll do it.

## After both

With the report and `roles.json`, I can close M0. That means recording the findings in
`SPEC.md`, fixing the y conversion if step 3 says so, and setting `ground_top_y` once S-L1
has a value. Then M1 (GridText) can start.
