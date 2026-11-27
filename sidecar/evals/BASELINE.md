# Eval baseline: openai_compatible deepseek-flash

2026-11-15, one seed per case, in two runs (`20261115-113753`: the first seven cases,
stopped by the 3M-token limit; `20261115-115555`: the other five). 4.96M tokens in all.

Scored against the cases as committed. Three soft assertions were tightened after the first
run, because the model passed them too easily; its runs are scored here against the new
ones: `cube-hard-2x` `min_window_ms` 12-60 (was a floor of 12), `cube-medium-orb`
`min_window_ms` 25-100, and `fairer-tricky` `objects_deleted` at most 4.

| case | hard | soft failed | rounds | tokens | time |
|---|---|---|---|---|---|
| cube-easy-empty | pass | - | 6 | 95k | 36 s |
| cube-hard-2x | pass | min_window_ms 104.2 (want 12-60): not hard | 38 | 1.73M | 351 s |
| cube-medium-orb | pass | - | 22 | 798k | 238 s |
| explain-difficulty | pass | - | 2 | 23k | 5 s |
| explain-wall | pass | - | 8 | 112k | 27 s |
| extend-pattern | pass | - | 6 | 90k | 25 s |
| fairer-tricky | pass | objects_deleted 8 (want at most 4) | 30 | 953k | 233 s |
| fix-slot12 | pass | - | 12 | 169k | 29 s |
| fix-wall | pass | - | 9 | 126k | 27 s |
| recolour | pass | - | 4 | 48k | 9 s |
| speed-change | pass | - | 14 | 222k | 49 s |
| two-turns | pass | - | 28 | 593k | 198 s |

Hard assertions: 12 of 12 cases. Soft: 2 cases with a failure.

Notes:

- Every case that edited verified after its last edit (R4).
- Cost is in the long cases: a build that tries, checks, deletes and tries again re-sends
  the whole turn each round (about 45k tokens a round by the end). Three cases use 70% of
  the tokens.
- `speed-change` built 30 columns of 2x with 6 objects: thin, but no assertion covers it.

# M5 baseline: game modes (openai_compatible deepseek-flash)

2026-11-17, run `m5-baseline`, one seed per case, 7.25M tokens. The seven M5 cases, scored
against the cases as committed. Their recordings are in `recordings/` and replay to the
same results.

| case | hard | soft failed | rounds | tokens | time |
|---|---|---|---|---|---|
| ball-mini | pass | - | 10 | 260k | 106 s |
| explain-robot | pass | - | 4 | 55k | 14 s |
| fix-ship-wall | pass | objects_deleted 10 (max 4), objects_created 5 (max 4), exit_mode ship | 14 | 488k | 240 s |
| ship-corridor | pass | - | 27 | 1.53M | 477 s |
| ship-insert | pass | - | 38 | 1.88M | 377 s |
| ufo-orbs | pass | - | 16 | 688k | 277 s |
| wave-easy | pass | min_window_ms 8.3 (min 40) | 30 | 2.35M | 469 s |

Hard assertions: 7 of 7 cases. Soft: 2 cases with a failure.

Notes:

- `fix-ship-wall`: the model cut the wall from 10 blocks to 5, which is a small fix. It
  deleted the column and placed the lower half again, so the counts look bigger than the
  change. The route it fixed flies over the low cube portal and ends as a ship. The model
  noticed this and offered to make the portal unavoidable. Before the fix,
  `verify_section` said `inconclusive`, not `impossible` (the ship-wall solver limit),
  and the model still worked out why the wall could not be passed.
- `wave-easy`: the model built a closed tunnel 3 blocks high. Its own check found a wide
  band (66 units), and its route still cleared the section with every tap 25 ms late. The
  8.3 ms comes from the solver's route, which reverses at the walls. The difficulty
  numbers measure the route the solver found, not the easiest one, and for the wave that
  understates how forgiving a section is. A solver issue, not the model's. Fixed the same
  day (spec §6.8.3, forgiving routes): replayed, the section now measures 75 ms on a
  route that keeps 18 units from the spikes and walls, at 5.7 inputs a second, so the soft
  assertion passes. The table keeps the run as recorded.
- `explain-robot`: it said plainly that the robot part cannot be checked, did not guess,
  and checked the cube stretches it could. Retired 2026-11-27: the simulator plays the
  robot since P19, so the case asked about something no longer unverifiable. Its case,
  fixture and recording are removed (in git history); `explain-green-orb` replaces it.
- In every case the model saw only the catalog seed (block and spike): the fake editor
  has no object table, and the real one comes from the game. It built portals, orbs and
  pads from GridText characters and the role table. Dumping the game's catalog into an
  eval fixture would make this closer to real use.
- `--max-total-tokens 5000000` checks between cases, so the last case ran past it.

# Sim-3 baseline: robot, spider, swing (openai_compatible deepseek-flash)

2026-11-27, one seed per case. Run `sim3-baseline` (3.49M tokens), then `robot-steps`
again as `sim3-robot-rerun` (326k) after the bug its first run found was fixed. The
recordings in `recordings/` are of the runs in the table, and replay to the same results.

| case | hard | soft failed | rounds | tokens | time |
|---|---|---|---|---|---|
| explain-green-orb | pass | - | 7 | 106k | 21 s |
| robot-steps (rerun) | pass | - | 14 | 326k | 92 s |
| spider-roof | pass | - | 18 | 660k | 177 s |
| swing-easy | pass | - | 35 | 2.39M | 475 s |

Hard assertions: 4 of 4 cases. Soft: none failed.

Notes:

- `robot-steps`, first run: the hard assertion `verified_after_last_edit` failed, and it
  was our bug, not the model's. A check that starts part-way (`place_grid`'s `verify`,
  or `verify_section` and `simulate_inputs` with an explicit start) still allowed only
  the upright cube, a leftover from Sim-1. Every check the model tried said the robot
  could not be simulated. It built the section, said plainly that it could not verify
  it, and noted that in the level plan, which was the right behaviour given what it was
  told. The section it left was playable (37.5 ms, 4 holds). Fixed the same day (an
  explicit start may be any simulated mode, upside down or mini, with `band_floor` for a
  raised band). The rerun verified its section: 116.7 ms, 4 holds, nothing frame-perfect.
- `spider-roof`: 122 objects, 18 spikes; 104 ms tightest window, 1.28 inputs a second.
- `swing-easy`: 50 ms tightest window, 0.69 inputs a second. 70% of the run's tokens:
  35 rounds of building and re-checking.
- `explain-green-orb`: it said the green orb cannot be checked, did not guess, and
  changed nothing.
