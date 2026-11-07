# Upgrading to a new Geometry Dash or Geode version

Several things in this project were **measured on one game version** rather than read from
an API. A new GD release can renumber or retype objects, add new ones, shift coordinates or
change physics, and none of that would fail loudly: the assistant would quietly reason with
stale facts. This checklist is how to re-measure them in order.

Currently measured on **GD 2.2091** (Geode 5.11.2), 2026-11-07.

## How you will notice

When the mod connects, the sidecar compares the game's version (from the hello) with the
version recorded in each data file, and logs a warning naming the file and the script
that refreshes it:

```
roles.json was measured on GD 2.2091 but the game is GD 2.300; refresh it with scripts/dump_roles.py
```

The data files are `sidecar/src/gd_ai_assistant/data/roles.json` and
`physics_constants.json`. A unit test also requires the two to record the **same** version,
so one can't be refreshed and the other forgotten.

## Checklist

All scripts run from `sidecar/`, with `start-sidecar.bat` closed and a level open in the
editor. See docs/M0-in-game-checks.md for how they behave.

1. **Bindings and build.** Update the Geode SDK, then `cmake --build build`. Compile errors
   here are renamed or removed bindings; fix them first. Restart the game so it loads the new
   build.
2. **Foundations.**

   ```bash
   uv run python scripts/check_foundations.py
   ```

   - The revision must move for every edit kind (S-L10).
   - Protocol y minus save y must be **0**. If it isn't, the 90-unit offset in
     `src/editor/properties.hpp` has changed. Check `docs/save-string-keys.md`.
3. **Fixes and ground.**

   ```bash
   uv run python scripts/check_m0_fixes.py
   ```

   - Every described object must report its expected type (`describe_unexpected` empty).
   - The ground block must read y = 15 (S-L1).
4. **Role table.**

   ```bash
   uv run python scripts/dump_roles.py
   ```

   It stamps the table with the new version and prints a **diff against the old table**:
   added, removed and changed ids. Review it:
   - **Added ids** are new objects. Check their roles look right.
   - **Changed roles or hitboxes** on existing ids are the dangerous ones: a level that
     used to verify may now not, or the reverse. Each needs a reason.
   - The sidecar logs `roles: unmapped GameObjectType N` for any new type in the game's enum.
     Add it to `_GAME_TYPES` in `design/roles.py`.
   - New objects the game types as the generic `Modifier` land in `special`. If one has
     gameplay meaning (like the speed portals) or is editor-only (like the guides), add
     it to `_MODIFIER_IDS`.
   - If the game crashes mid-dump, the last `describe_object_types: id N` line in the Geode
     log names the id. Add it to `kDescribeDenylist` in `src/exec/design_jobs.cpp`.
5. **Physics constants.** Until `scripts/calibrate.py` exists (M2), re-check `ground_top_y`
   (step 3). Mark speeds and other values `guess` again if the release notes mention
   physics. Then set `game_version` in `physics_constants.json` to the new version.
6. **Tests.** Run `uv run pytest -q`. It fails if the two data files disagree on the version.
7. **Commit** the refreshed data on its own, with the version in the message
   ("data: re-measure for GD 2.300"), so a regression can be bisected to the upgrade.

## What is *not* covered yet

- **Physics** (gravity, jump, orb impulses): measured in M2 by the calibration script.
  Until then, the speeds in `physics_constants.json` are guesses either way.
- **Save-string keys** (`docs/save-string-keys.md`, `properties_table.py`): there is no
  automated re-check. New keys show up as unknown in `get_object_properties`.
