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

## 2. Role dump: S-L2, in full

```bash
uv run python scripts/dump_roles.py
```

This instantiates every object type off-scene, a few thousand of them. Nothing is added to
the level. It writes `sidecar/src/gd_ai_assistant/data/roles.json` and prints counts by
role and a few spot checks. That file is meant to be committed.

## 3. If the game crashes during the dump

The mod logs `describe_object_types: id N` **before** creating each object, so the last
such line in the newest file under `<Geometry Dash>\geode\logs\` names the culprit. Add
that id to `kDescribeDenylist` in `src/exec/design_jobs.cpp` (with a comment), rebuild, and
rerun. Or just send me the id and I'll do it.

## After both

With the report and `roles.json`, I can close M0. That means recording the findings in
`SPEC.md`, fixing the y conversion if step 3 says so, and setting `ground_top_y` once S-L1
has a value. Then M1 (GridText) can start.
