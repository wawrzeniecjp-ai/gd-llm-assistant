"""The solver's speed against the M2 budget (LEVEL-DESIGN-SPEC §6.8.4: a 1000-column cube
section in at most 2 s).

The level is the ten possible M2 sections (slots 1-10, columns 100-1099) solved as one
run from slot 1's start position, plus each slot on its own as `verify_section` sees it.

    uv run python scripts/bench_solver.py
"""

from __future__ import annotations

import statistics
import time

from build_sections import payloads, verify_args
from m2_sections import SECTIONS

from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.coords import Grid
from gd_ai_assistant.design.roles import load_role_table
from gd_ai_assistant.design.sim.cube import Cube, CubeParams
from gd_ai_assistant.design.sim.solver import solve
from gd_ai_assistant.design.sim.verify import verify
from gd_ai_assistant.design.sim.world import world_from_objects

BUDGET_S = 2.0
REPEATS = 3


def main() -> None:
    grid = Grid.from_constants()
    possible = [s for s in SECTIONS if s.expected == "possible"]
    objects = [p for s in possible for p in payloads(s, grid)]
    world = world_from_objects(objects, load_role_table())
    params = CubeParams.from_constants(load_constants())

    start_x = grid.centre_x(possible[0].col + 2)
    end_x = grid.centre_x(possible[-1].col + 99)
    times, result = [], None
    for _ in range(REPEATS):
        began = time.perf_counter()
        result = solve(world, params, Cube(x=start_x, y=15.0, grounded=True), end_x,
                       budget_s=30.0)
        times.append(time.perf_counter() - began)
    best = min(times)
    columns = round((end_x - start_x) / 30)
    verdict = "within" if best <= BUDGET_S else "OVER"
    print(f"slots 1-10 as one run, {columns} columns: {result.result}, best {best:.2f} s "
          f"of {REPEATS} (median {statistics.median(times):.2f} s), {verdict} the "
          f"{BUDGET_S:.0f} s budget")

    print("each slot as verify_section sees it (solve + windows):")
    for section in SECTIONS:
        began = time.perf_counter()
        answer = verify(world_from_objects(payloads(section, grid), load_role_table()),
                        params, verify_args(section.slot))
        print(f"  slot {section.slot:2}: {answer['result']:12} "
              f"{(time.perf_counter() - began) * 1000:7.0f} ms")


if __name__ == "__main__":
    main()
