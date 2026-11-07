"""Dump the object role table from the running game (LEVEL-DESIGN-SPEC.md 4.2, spike S-L2).

Every object type is instantiated **off-scene** by the mod (`describe_object_types`) and
described: its `GameObjectType`, hitbox and flags. Nothing is added to the level.

Run from `sidecar/`, with the game in the level editor and `start-sidecar.bat` closed:

    uv run python scripts/dump_roles.py

Writes `src/gd_ai_assistant/data/roles.json` and prints a summary. If the game crashes,
the last `describe_object_types: id N` line in the Geode log names the id that did it;
add it to `kDescribeDenylist` in `src/exec/design_jobs.cpp` and run again.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

from _harness import run

from gd_ai_assistant.design.roles import _default_path, fetch_role_table
from gd_ai_assistant.server import SidecarServer

GAME_VERSION = "2.2091"


async def routine(server: SidecarServer) -> None:
    def progress(done: int, total: int) -> None:
        print(f"\r  described {done}/{total}", end="", file=sys.stderr, flush=True)

    table, failed = await fetch_role_table(server, game_version=GAME_VERSION, progress=progress)
    print(file=sys.stderr)

    data = table.to_json()
    data["failed"] = sorted(failed)
    path: Path = _default_path()
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")

    by_role = collections.Counter(entry.role.value for entry in table.entries.values())
    print(f"wrote {path} -- {len(table)} ids, {len(failed)} refused")
    for role, count in sorted(by_role.items(), key=lambda kv: -kv[1]):
        print(f"  {role:16} {count}")
    for object_id in (1, 8, 36, 39, 103):
        entry = table.entries.get(object_id)
        if entry is not None:
            print(f"  spot check id {object_id}: {entry.role.value} {entry.subrole or ''} "
                  f"{entry.variant or ''} hitbox={entry.hitbox} frame={entry.frame}")


if __name__ == "__main__":
    run(routine)
