"""Place the 20 M2 test sections (scripts/m2_sections.py) in the open level.

Run from `sidecar/`, with `start-sidecar.bat` closed and an empty level open in the
editor:

    uv run python scripts/build_sections.py              # place all 20
    uv run python scripts/build_sections.py 3 13         # place only slots 3 and 13
    uv run python scripts/build_sections.py --replace 3  # rebuild slot 3: deletes what is
                                                         # in its 100 columns first
    uv run python scripts/build_sections.py --offline    # no game: verify the drafts

Without `--replace`, a slot that already holds objects is skipped, so your edits are
never overwritten by accident. The objects are placed exactly as the GridText says, with
no model in between. Afterwards, `scripts/check_sections.py` checks what is in the level.
"""

from __future__ import annotations

import sys
from typing import Any

from m2_sections import SECTIONS, SLOT_COLS, Section

from gd_ai_assistant.design.constants import load_constants
from gd_ai_assistant.design.coords import Grid
from gd_ai_assistant.design.grid import parse, plan_placement
from gd_ai_assistant.design.roles import Role, load_role_table
from gd_ai_assistant.design.sim.cube import CubeParams
from gd_ai_assistant.design.sim.verify import verify
from gd_ai_assistant.design.sim.world import world_from_objects

START_COL = 2
END_COL = 95


def payloads(section: Section, grid: Grid) -> list[dict[str, Any]]:
    return plan_placement(parse(section.grid_text()), {}, grid, load_role_table()).payloads


def verify_args(slot: int) -> dict[str, Any]:
    x_slot = slot * SLOT_COLS * 30
    return {"x_range": [x_slot + START_COL * 30, x_slot + END_COL * 30],
            "start_from": "explicit", "start": {"mode": "cube", "speed": "1x", "row": 0}}


def offline(chosen: list[Section]) -> int:
    """Verify the drafts in a simulated level holding only them."""
    grid = Grid.from_constants()
    params = CubeParams.from_constants(load_constants())
    objects = [p for s in SECTIONS for p in payloads(s, grid)]
    world = world_from_objects(objects, load_role_table())
    wrong = 0
    for section in chosen:
        result = verify(world, params, verify_args(section.slot))
        got = result["result"]
        mark = "ok   " if got == section.expected else "WRONG"
        if got == "possible":
            detail = (f"{len(result['route'])} presses, tightest window "
                      f"{result['difficulty']['min_window_ms']} ms")
        else:
            col = int(result.get("furthest_x", 0) // 30) - section.slot * SLOT_COLS
            detail = f"furthest column {col}" + (f" ({result['reason']})"
                                                 if result.get("reason") else "")
        print(f"  slot {section.slot:2} {mark} {got:12} {section.title}: {detail}")
        wrong += got != section.expected
    return wrong


async def place(server: Any, chosen: list[Section], replace: bool) -> dict[str, Any]:
    from _harness import ok

    from gd_ai_assistant.design.level_model import load_level_model

    grid = Grid.from_constants()
    model = await load_level_model(server, load_role_table())
    placed, skipped = [], []
    for section in chosen:
        x0 = section.col * 30
        existing = [o for o in model.in_rect(x0, -1e9, x0 + SLOT_COLS * 30, 1e9)
                    if o.is_gameplay or o.role is Role.START_POS]
        if existing and not replace:
            print(f"  slot {section.slot:2}: already holds {len(existing)} objects, skipped "
                  "(use --replace to rebuild it)")
            skipped.append(section.slot)
            continue
        if existing:
            await ok(server, "delete_objects", {"targets": [o.uid for o in existing]})
        objects = payloads(section, grid)
        await ok(server, "create_objects", {"objects": objects})
        print(f"  slot {section.slot:2}: placed {len(objects)} objects"
              + (f" (replaced {len(existing)})" if existing else "")
              + f" -- {section.title}")
        placed.append(section.slot)
    print(f"\nPlaced {len(placed)} slots" + (f", skipped {skipped}" if skipped else "")
          + ". Save the level in the editor.")
    return {"placed": placed, "skipped": skipped}


def main() -> None:
    args = sys.argv[1:]
    replace = "--replace" in args
    is_offline = "--offline" in args
    slots = {int(a) for a in args if a.isdigit()}
    chosen = [s for s in SECTIONS if not slots or s.slot in slots]
    if is_offline:
        raise SystemExit(1 if offline(chosen) else 0)
    from _harness import run

    run(lambda server: place(server, chosen, replace))


if __name__ == "__main__":
    main()
