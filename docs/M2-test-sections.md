# M2 test sections: 10 possible, 10 impossible

M2 is done when the solver classifies 20 hand-made cube sections correctly
(LEVEL-DESIGN-SPEC.md §21). `scripts/build_sections.py` places 20 drafts to start from;
you play them, push them closer to the edge by hand, and `scripts/check_sections.py`
reads the level and checks each one.

## Starting from the drafts

The drafts are in `sidecar/scripts/m2_sections.py`, one GridText block per slot, built as
pairs: slot k+10 is slot k one notch harder. The solver classifies all 20 as intended
(`tests/test_m2_sections.py` keeps it that way). From `sidecar/`, with `start-sidecar.bat`
closed and a **new, empty level** open in the editor:

```bash
uv run python scripts/build_sections.py
```

Then save the level. The script places the objects exactly as the text says, with no
model in between. It never overwrites: a slot that already holds objects is skipped.
To rebuild one slot from its draft, deleting whatever is in its 100 columns:
`uv run python scripts/build_sections.py --replace 4`. `--offline` checks the drafts
without the game.

The drafts only encode what the simulator already believes, so they are not the test by
themselves. The test is you: **beat every possible section by hand**, and give every
impossible one a few honest tries. Beating an impossible one, or failing a possible one
whose tightest window is generous, is a simulator bug, and exactly what we're after.

## Moving objects off the grid

Allowed and useful. The check reads real positions, not columns, so an object nudged with
the editor's small move buttons is simulated exactly where it is. That is the finest way
to find an edge: a spike moved 5 units until the section flips. Keep rotations to right
angles and scale at 1. Two things to know: `render_grid` shows an off-grid object as `?`
(that does not affect the check), and `--replace` on a slot throws your nudges away.

## The level

**One new level, 20 sections side by side**, each in its own fixed slot of 100 columns:

| Slot | Columns | x (units) | Meant to be |
| --- | --- | --- | --- |
| 1 | 100-199 | 3000-5999 | possible |
| 2 | 200-299 | 6000-8999 | possible |
| … | … | … | … |
| 10 | 1000-1099 | 30000-32999 | possible |
| 11 | 1100-1199 | 33000-35999 | **impossible** |
| … | … | … | … |
| 20 | 2000-2099 | 60000-62999 | **impossible** |

Slot *k* starts at column `100 × k`. Columns 0-99, before slot 1, stay empty. **Slots
1-10 are your possible sections and 11-20 your impossible ones**, so you don't need to
write down which is which.

The editor shows each object's x in the Edit Object window, and the camera position.
Column *c* spans x = 30c to 30c + 30, so slot 7 starts at x = 21000.

Inside each slot:

| Slot columns | Use |
| --- | --- |
| 0-9 | **Runway.** Flat ground, nothing on it. The check starts the cube here (column 2) on the ground at 1x speed. Put a **start position** here too (see below) so you can playtest the section on its own. |
| 10-89 | **The section.** Up to 80 columns of obstacles. |
| 90-99 | **Buffer.** Leave empty. The check ends at column 95. |

Each section is checked on its own, from its own runway. What happens in other slots
doesn't matter, and the cube always enters a section at 1x on the ground.

## What you can use

Only what the simulator handles (Sim-1). Anything else in a slot makes it
`unverifiable`, which counts as a failed check.

- **Solids:** the default block and other plain blocks, half slabs. Platforms can float.
- **Hazards:** spikes in any of the four directions, small spikes, saws.
- **Yellow and pink orbs**, **yellow and pink pads**.
- **Speed portals** (0.5x to 4x), to test a section at another speed. **Put it in the
  runway, at column 5, on the ground**, and keep the section's first obstacle at least 10
  columns after it (column 15 or later). The cube then crosses the portal on the ground.
  A portal right next to obstacles is legitimate in a real level, but it lets the cube jump
  over the portal and keep its old speed. That makes the search much larger, and the
  current solver may answer `inconclusive` there.
- Start positions (ignored by the check) and any decoration (ignored).

Not allowed in a slot: mode portals (ship, ball and so on), gravity or size portals, blue,
green, red, black or dash orbs, blue pads, slopes, triggers, moving objects, or anything
rotated at an angle that isn't a multiple of 90°.

## Making the sections good tests

The most useful sections sit close to the edge, so that a wrong physics constant would
flip the answer. The simulator's current limits, all at 1x on flat ground:

- A row of **3 spikes** is possible; **4** is impossible.
- A **2-block step** can be jumped onto; **3 blocks** can't.
- From a 1-block platform, **4 spikes** after it can be cleared; **5** can't.
- A gap of **5 spikes** between two 1-block platforms is possible; **6** isn't.
- A corridor with its ceiling **3 blocks** up still lets you jump a spike; **2 blocks** up
  doesn't.
- A yellow pad clears a **4-block** wall with one empty column between them; flush
  against the wall it doesn't. A pink pad doesn't at any distance.
- A yellow orb in row 3 over a 7-spike pit (orb in the pit's second column) is reachable;
  in row 4 it isn't. 8 spikes can't be crossed with one orb.
- The longest row of spikes, with a speed portal in the runway:

  | Speed | 0.5x | 1x | 2x | 3x | 4x |
  | --- | --- | --- | --- | --- | --- |
  | Possible | 2 | 3 | 4 | 5 | 6 |
  | Impossible | 3 | 4 | 5 | 6 | 7 |

So pairs work well: the same idea in a possible slot and, one notch harder, in an
impossible one. Some ideas:

| Possible | Impossible (one notch further) |
| --- | --- |
| 3 spikes in a row | 4 spikes in a row |
| a 2-block step up | a 3-block step |
| a yellow orb over a 6-spike pit | the same pit with the orb a row too high to reach |
| a yellow pad launching over a 4-block wall | a pink pad (weaker) before the same wall |
| platforms with a 4-block gap between them | the same with a 6-block gap |
| a staircase of 1-block steps with spikes between them | the same with one step 3 blocks high |
| a corridor (ceiling 3 blocks up) with a single spike | the same corridor with a ceiling 2 blocks up |
| 2x portal in the runway, then 4 spikes | 2x, then 5 spikes |
| 0.5x portal in the runway, then 2 spikes | 0.5x, then 3 spikes |
| a pink orb hop onto a 2-block platform | needing a yellow orb's height but given a pink one |
| a long mixed section: 6–8 obstacles in sequence | one impossible obstacle hidden in the middle of a long section |

Treat these as starting points and mix in your own. Sections that look hard but turn out
possible, or look easy but turn out impossible, are especially good.

**Check your possible sections by playing them.** Put a start position in the runway
(columns 2-5, on the ground, 1x speed), playtest from it, and beat the section. If you
can't, it doesn't belong in slots 1-10. Nobody can prove an impossible one by failing it,
so make those clearly over the line, or one notch past a possible twin.

## Running the check

With the level open in the editor, `start-sidecar.bat` closed, from `sidecar/`:

```bash
uv run python scripts/check_sections.py
```

It only reads the level: nothing is played or changed, and it takes a few seconds. You can
run it after every few sections; empty slots are skipped. It prints one line per slot:

```
  slot  1: expected possible   got possible      PASS  1 presses, tightest window 220.8 ms
  slot 11: expected impossible got impossible    PASS  furthest x 33420 (column 14 of the slot), killed by [812]
```

and writes `build/sections-report.json`. Send me that file. A `FAIL` is what the exercise is
for. Either the section isn't what you meant (the furthest x and the killing object say
where to look), or the simulator is wrong, which is what I need to know.
