"""Build the seed pattern library (LEVEL-DESIGN-SPEC.md 10.5).

The seed patterns are authored here, as GridText, and written to
`src/gd_ai_assistant/data/patterns/seed/` only if the simulator plays them:

- each is verified at 1x from its entry state (`patterns.verify_pattern`);
- its difficulty is the easiest band (§14.3) its tightest window meets, so the number is
  measured, not guessed;
- its exit is the state it actually leaves, which every variant must keep;
- the other speeds are made with `rescale_speed` and committed only if they verify.

Run from `sidecar/` after changing a pattern or the physics constants:

    uv run python scripts/build_seed_patterns.py            # build and write
    uv run python scripts/build_seed_patterns.py --check    # report only, write nothing
"""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import Any

from gd_ai_assistant.design import patterns as P
from gd_ai_assistant.design.bands import load_bands
from gd_ai_assistant.design.constants import SPEEDS, load_constants

CHECK = "--check" in sys.argv
#: One tick, in ms (with rounding): a window this narrow is frame-perfect (§14.2).
FRAME_TICK_MS = 4.2


def grid(*rows: str) -> str:
    """A relative GridText block from its rows, top first (all the same width)."""
    width = len(rows[0])
    assert all(len(r) == width for r in rows), rows
    return "\n".join([f"@grid col=0 row=0 w={width} h={len(rows)}", *rows])


def air(height: int, *rows: str) -> tuple[str, ...]:
    """Pad a flight pattern's rows (given top first, ending at row 0) to `height` with
    empty rows on top, so it fills its band."""
    width = len(rows[0])
    return ("." * width,) * (height - len(rows)) + rows


SEEDS: list[dict[str, Any]] = []


def seed(base: str, name: str, mode: str, rows: tuple[str, ...] | list[str], tags: list[str],
         *, entry: dict[str, Any] | None = None, floor: str | None = "#",
         gravity: int = 1, mini: bool = False) -> None:
    SEEDS.append({"base": base, "name": name, "mode": mode, "rows": tuple(rows),
                  "tags": [mode, *tags], "entry": entry, "floor": floor,
                  "gravity": gravity, "mini": mini})


GROUNDED = {"row": 0, "grounded": True, "vy": 0}
SHIP_ENTRY = {"row": 3, "grounded": False, "vy": 0}

# -- cube (25) --------------------------------------------------------------------------
E = "." * 14
seed("cube.single-spike", "Single spike", "cube",
     (E, E, E, "......^......."), ["spike", "single-input", "intro"])
seed("cube.double-spike", "Double spike", "cube",
     (E, E, E, "......^^......"), ["spike", "single-input"])
seed("cube.triple-spike", "Triple spike", "cube",
     (E, E, E, ".....^^^......"), ["spike", "single-input", "classic"])
seed("cube.spike-pair-rhythm", "Two spikes in rhythm", "cube",
     ("." * 16,) * 3 + ("....^......^....",), ["spike", "rhythm"])
seed("cube.spike-triplet-rhythm", "Three spikes in rhythm", "cube",
     ("." * 20,) * 3 + ("...^.....^.....^....",), ["spike", "rhythm"])
seed("cube.double-then-single", "Double spike, then single", "cube",
     ("." * 18,) * 3 + ("...^^.......^.....",), ["spike", "rhythm"])
seed("cube.block-hop", "Hop over a block", "cube",
     (E, E, E, "......#......."), ["block", "single-input", "intro"])
seed("cube.step-up", "Step up onto blocks", "cube",
     (E, E, E, "......########"), ["block", "step", "landing"])
seed("cube.step-up-down", "Up a step and off it", "cube",
     ("." * 18,) * 3 + ("......#####.......",), ["block", "step", "landing"])
seed("cube.two-step-stair", "Two-step stair", "cube",
     ("." * 16,) * 2 + ("..........######", "......##########"), ["block", "stair"])
seed("cube.three-step-stair", "Three-step stair", "cube",
     ("." * 20,) * 2 + ("..............######", "..........##########",
                       "......##############"), ["block", "stair"])
seed("cube.spike-on-block", "Spike on a block", "cube",
     ("." * 16,) * 2 + (".......^........", "......###......."), ["spike", "block"])
seed("cube.blocks-over-spikes", "Block islands over spikes", "cube",
     ("." * 18,) * 3 + ("....##^^^##^^^##..",), ["block", "spike", "rhythm"])
seed("cube.spike-under-ceiling", "Spike under a low ceiling", "cube",
     ("....########....", "................", "................", "........^......."),
     ["spike", "ceiling", "timing"])
seed("cube.wall-two-high", "Two-high wall", "cube",
     (E, E, "......#.......", "......#......."), ["block", "wall"])
seed("cube.spike-before-block", "Spike, then a block", "cube",
     ("." * 16,) * 3 + ("....^....#......",), ["spike", "block", "rhythm"])
seed("cube.yellow-orb-pit", "Yellow orb over a pit", "cube",
     ("." * 20,) * 2 + ("........o...........", "....................",
                       ".......^^^^^^......."), ["orb", "pit", "two-input"])
seed("cube.pink-orb-hop", "Pink orb hop", "cube",
     ("." * 16,) * 2 + ("........p.......", "................", ".......^^^......"),
     ["orb", "pit"])
seed("cube.yellow-pad-launch", "Yellow pad over spikes", "cube",
     ("." * 20,) * 3 + ("....._^^^^^.........",), ["pad", "pit"])
seed("cube.pink-pad-hop", "Pink pad hop", "cube",
     ("." * 16,) * 3 + ("....._^^^.......",), ["pad", "no-input"])
seed("cube.small-spikes", "Row of small spikes", "cube",
     (E, E, E, "......,,,....."), ["spike", "small"])
seed("cube.spike-block-spike", "Spike, block, spike", "cube",
     ("." * 18,) * 3 + ("...^....#....^....",), ["spike", "block", "rhythm"])
seed("cube.double-into-step", "Double spike into a step", "cube",
     ("." * 18,) * 3 + ("....^^......######",), ["spike", "block", "landing"])
seed("cube.saw", "Saw blade on the floor", "cube",
     (E, E, E, "......*......."), ["saw", "hazard", "single-input"])
seed("cube.gap-jump", "Jump between two blocks", "cube",
     ("." * 16,) * 3 + ("....####^^####..",), ["block", "pit", "landing"])

# -- ship (10): the band is 10 rows from the ground; the ship enters on row 3 --------------
W = 18
S = "." * W
seed("ship.floor-spikes", "Ship over floor spikes", "ship",
     air(10, "....^^^^^^^^......"), ["spike", "intro"], entry=SHIP_ENTRY)
seed("ship.low-ceiling", "Ship under a low ceiling", "ship",
     air(10, "....########......", S, S, S, S, "....^^^^^^^^......"),
     ["ceiling", "corridor"], entry=SHIP_ENTRY)
seed("ship.block-pillar", "Ship over a pillar", "ship",
     air(10, ".......#..........", ".......#..........", ".......#..........",
         ".......#.........."), ["block", "pillar"], entry=SHIP_ENTRY)
seed("ship.hanging-pillar", "Ship under a hanging pillar", "ship",
     (".......#..........",) * 4 + (S,) * 6, ["block", "pillar"], entry=SHIP_ENTRY)
seed("ship.slalom", "Ship slalom", "ship",
     ("......#...........", "......#...........", "......#...........", S, S, S,
      "...........#......", "...........#......", "...........#......",
      "...........#......"), ["block", "slalom"], entry=SHIP_ENTRY)
seed("ship.narrow-gap", "Ship through a gap", "ship",
     ("........#.........",) * 3 + (S,) * 3 + ("........#.........",) * 4,
     ["block", "gap"], entry=SHIP_ENTRY)
seed("ship.spike-corridor", "Ship spike corridor", "ship",
     ("....vvvvvvvvv.....", S, S, S, S, S, S, S, S, "....^^^^^^^^^....."),
     ["spike", "corridor"], entry=SHIP_ENTRY)
seed("ship.rising-steps", "Ship climbing over steps", "ship",
     air(10, "...........###....", "........######....", ".....#########...."),
     ["block", "climb"], entry=SHIP_ENTRY)
seed("ship.yellow-orb", "Ship yellow orb", "ship",
     air(10, ".......o..........", S, S, "....^^^^^^^^......"), ["orb"],
     entry=SHIP_ENTRY)
seed("ship.saw-pair", "Ship between two saws", "ship",
     air(10, "......*...........", S, S, S, "..........*......."), ["saw", "hazard"],
     entry=SHIP_ENTRY)

# -- ball (8): the band is 8 rows; spikes on both sides ------------------------------------
B = "." * 18
BV = "v" * 18
seed("ball.floor-spike", "Ball over a floor spike", "ball",
     (B,) * 7 + (".......^..........",), ["spike", "flip"], entry=GROUNDED)
seed("ball.ceiling-spike", "Ball under a ceiling spike", "ball",
     (".......v..........",) + (B,) * 7, ["spike", "flip"], entry=GROUNDED)
seed("ball.alternate", "Ball alternating sides", "ball",
     (".........vvv......",) + (B,) * 6 + ("....^^^...........",), ["spike", "flip", "rhythm"],
     entry=GROUNDED)
seed("ball.floor-run", "Ball over a spike run", "ball",
     (B,) * 7 + ("....^^^^^^^.......",), ["spike", "flip"], entry=GROUNDED)
seed("ball.block-step", "Ball onto a block", "ball",
     (B,) * 7 + ("......######......",), ["block", "landing"], entry=GROUNDED)
seed("ball.zigzag", "Ball zigzag", "ball",
     ("......vvv.....vvv.",) + (B,) * 6 + ("...^^^.....^^^....",), ["spike", "flip", "rhythm"],
     entry=GROUNDED)
seed("ball.yellow-pad", "Ball yellow pad", "ball",
     (B,) * 7 + ("....._^^^^^.......",), ["pad", "no-input"], entry=GROUNDED)
seed("ball.blue-orb", "Ball blue orb", "ball",
     (B,) * 4 + (".......b..........",) + (B,) * 2 + ("......^^^^^^......",),
     ["orb", "blue", "flip"], entry=GROUNDED)

# -- ufo (6): the band is 10 rows ---------------------------------------------------------
seed("ufo.spike-hop", "UFO spike hop", "ufo",
     air(10, ".....^^^^^........"), ["spike", "taps"], entry=GROUNDED)
seed("ufo.pillar", "UFO over a pillar", "ufo",
     air(10, "........#.........", "........#.........", "........#........."),
     ["block", "pillar", "taps"], entry=GROUNDED)
seed("ufo.low-ceiling", "UFO under a low ceiling", "ufo",
     air(10, "....##########....", S, S, S, "......^^^^^^......"), ["ceiling", "taps"],
     entry=GROUNDED)
seed("ufo.steps", "UFO up steps", "ufo",
     air(10, "...........######.", "........#########.", ".....############."),
     ["block", "climb", "taps"], entry=GROUNDED)
seed("ufo.yellow-orb", "UFO yellow orb", "ufo",
     air(10, ".......o..........", S, S, "....^^^^^^^^......"), ["orb", "taps"],
     entry=GROUNDED)
seed("ufo.gap", "UFO through a gap", "ufo",
     ("........#.........",) * 4 + (S,) * 3 + ("........#.........",) * 3,
     ["block", "gap", "taps"], entry=GROUNDED)

# -- wave (6): the band is 10 rows; the wave dies on any block it touches ----------------
seed("wave.floor-spikes", "Wave over floor spikes", "wave",
     air(10, "....^^^^^^^^......"), ["spike", "intro"], entry=GROUNDED)
seed("wave.pillar", "Wave over a pillar", "wave",
     air(10, "........#.........", "........#.........", "........#........."),
     ["block", "pillar"], entry=GROUNDED)
seed("wave.hanging-pillar", "Wave under a hanging pillar", "wave",
     ("........#.........",) * 3 + (S,) * 7, ["block", "pillar"], entry=GROUNDED)
seed("wave.gap", "Wave through a gap", "wave",
     ("........#.........",) * 4 + (S,) * 3 + ("........#.........",) * 3,
     ["block", "gap"], entry=GROUNDED)
seed("wave.zigzag", "Wave zigzag", "wave",
     ("......#...........", "......#...........", "......#...........", S, S, S, S,
      "...........#......", "...........#......", "...........#......"),
     ["block", "slalom"], entry=GROUNDED)
seed("wave.spike-corridor", "Wave spike corridor", "wave",
     ("....vvvvvvvvv.....", S, S, S, S, S, S, S, S, "....^^^^^^^^^....."),
     ["spike", "corridor"], entry=GROUNDED)

# -- transitions (5): a portal in, a short stretch, a portal out. The exit portal is walled
# in above, so a route cannot fly over it and stay in the mode.
T = "." * 24
WALLED = "." * 14 + "#" + "." * 9


def walled(portal_in: str) -> tuple[str, ...]:
    return ((WALLED,) * 7 + (T, f"...{portal_in}..........C.........",
                             "........^^^^............"))


seed("transition.cube-ship-cube", "Into the ship and back", "cube", walled("S"),
     ["transition", "portal"], entry=GROUNDED)
seed("transition.cube-ball-cube", "Into the ball and back", "cube", walled("B"),
     ["transition", "portal"], entry=GROUNDED)
seed("transition.cube-ufo-cube", "Into the UFO and back", "cube", walled("U"),
     ["transition", "portal"], entry=GROUNDED)
seed("transition.cube-wave-cube", "Into the wave and back", "cube", walled("W"),
     ["transition", "portal"], entry=GROUNDED)
seed("transition.cube-gravity-flip", "Gravity flip under a roof and back", "cube",
     ("#" * 24, "........v.....&.........", T, T, T, T, T, T,
      "...+....................", T), ["transition", "gravity"], entry=GROUNDED)

# -- robot, spider, swing (2 each): Sim-3 ------------------------------------------------
seed("robot.two-step", "Robot up a two-block step", "robot",
     ("." * 16,) * 3 + ("......##########", "......##########"), ["block", "hold"],
     entry=GROUNDED)
seed("robot.spike-pit", "Robot over a spike pit", "robot",
     ("." * 18,) * 4 + ("......^^^^........",), ["spike", "hold"], entry=GROUNDED)
seed("spider.floor-spikes", "Spider over floor spikes", "spider",
     ("#" * 18,) + ("." * 18,) * 5 + ("......^^^^^^......",), ["spike", "teleport", "roof"],
     entry=GROUNDED)
seed("spider.switch-sides", "Spider switching sides", "spider",
     ("#" * 22, "..........vvvvv.......") + ("." * 22,) * 4
     + ("....^^^^^.............",), ["spike", "teleport", "roof"], entry=GROUNDED)
seed("swing.floor-spikes", "Swing over floor spikes", "swing",
     air(10, "....^^^^^^^^......"), ["spike", "flip"], entry=GROUNDED)
seed("swing.corridor", "Swing spike corridor", "swing",
     ("....vvvvvvvvv.....", S, S, S, S, S, S, S, S, "....^^^^^^^^^....."),
     ["spike", "corridor"], entry=GROUNDED)


# -- building ---------------------------------------------------------------------------------


def difficulty_for(mode: str, window: float | None) -> int:
    """The easiest band whose floor the tightest window meets (1 with no inputs)."""
    if window is None:
        return 1
    bands = load_bands()
    for level in range(1, 11):
        if window >= bands.band(level, mode).min_window_ms:
            return level
    return 10


def build(spec: dict[str, Any]) -> tuple[P.Pattern | None, str]:
    pattern = P.Pattern.from_json({
        "schema": P.SCHEMA, "id": f"{spec['base']}.1x", "name": spec["name"],
        "tags": spec["tags"], "mode": spec["mode"], "speed": "1x",
        "gravity": spec["gravity"], "mini": spec["mini"], "difficulty": 1,
        "grid": grid(*spec["rows"]), "floor": spec["floor"],
        "entry": spec["entry"] or GROUNDED, "exit": {},
        "source": {"kind": "seed", "author": "gd-ai-assistant", "license": "MIT"},
    }, where=spec["base"])
    check = P.verify_pattern(pattern)
    # The first pass has no exit to compare with yet: a mismatch only means it leaves in
    # some other state, which becomes its declared exit.
    if check["result"] not in ("possible", "exit_mismatch") or "exit" not in check:
        return None, f"{check['result']}: {check.get('reason', '')}"
    window = check.get("min_window_ms")
    if window is not None and window <= FRAME_TICK_MS:
        return None, f"frame-perfect ({window} ms): not a seed"
    final = check["exit"]
    exit_state: dict[str, Any] = {"mode": final["mode"], "gravity": final["gravity"],
                                  "grounded": final["grounded"]}
    if final["grounded"]:
        from gd_ai_assistant.design.sim.cube import Cube, CubeParams, half_of
        params = CubeParams.from_constants(load_constants())
        half = half_of(Cube(mode=final["mode"], mini=pattern.mini), params)
        exit_state["row"] = round((final["y"] - half) / 30) if final["gravity"] > 0 \
            else round((final["y"] + half) / 30) - 1
    pattern = replace(pattern, exit=exit_state,
                      difficulty=difficulty_for(pattern.mode, check.get("min_window_ms")))
    pattern = P.verified(pattern)
    assert pattern.verified is not None
    if pattern.verified["result"] != "possible":
        return None, f"exit: {pattern.verified.get('reason')}"
    return pattern, f"{pattern.verified['min_window_ms']} ms, difficulty {pattern.difficulty}"


def main() -> None:
    out_dir = P.seed_dir()
    built: list[P.Pattern] = []
    failed = 0
    for spec in SEEDS:
        pattern, note = build(spec)
        print(f"{spec['base']:34} 1x  {'ok ' if pattern else 'FAIL'} {note}")
        if pattern is None:
            failed += 1
            continue
        built.append(pattern)
        for speed in SPEEDS:
            if speed == "1x":
                continue
            try:
                other = P.rescaled(pattern, speed)
            except P.PatternError as exc:
                print(f"{'':34} {speed:4} skipped: {str(exc).split(': ', 1)[-1][:70]}")
                continue
            window = (other.verified or {}).get("min_window_ms")
            if window is not None and window <= FRAME_TICK_MS:
                print(f"{'':34} {speed:4} skipped: frame-perfect ({window} ms)")
                continue
            other = replace(other, difficulty=difficulty_for(other.mode, window))
            built.append(P.verified(other))
    by_mode: dict[str, int] = {}
    for p in built:
        if p.speed == "1x":
            key = "transition" if p.id.startswith("transition.") else p.mode
            by_mode[key] = by_mode.get(key, 0) + 1
    print(f"\n{sum(by_mode.values())} patterns at 1x ({failed} failed), {len(built)} with "
          f"other speeds: {by_mode}")
    if CHECK:
        return
    for old in out_dir.glob("*.json") if out_dir.exists() else []:
        old.unlink()
    for p in built:
        P.save_pattern(p, out_dir)
    print(f"written to {out_dir}")


if __name__ == "__main__":
    main()
