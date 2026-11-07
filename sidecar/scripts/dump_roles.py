"""Dump the object role table from the running game (LEVEL-DESIGN-SPEC.md 4.2, spike S-L2).

Every object type is instantiated **off-scene** by the mod (`describe_object_types`) and
described: its `GameObjectType`, hitbox and flags. Nothing is added to the level.

Run from `sidecar/`, with the game in the level editor and `start-sidecar.bat` closed:

    uv run python scripts/dump_roles.py

Writes `src/gd_ai_assistant/data/roles.json`, stamped with the connected game's version,
and prints a summary plus a **diff against the table it replaces** (new, removed and
changed ids), which is what to review after a game update (docs/UPGRADING.md). If the
game crashes,
the last `describe_object_types: id N` line in the Geode log names the id that did it;
add it to `kDescribeDenylist` in `src/exec/design_jobs.cpp` and run again.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

from _harness import run

from gd_ai_assistant.design.roles import (
    RoleTable,
    _default_path,
    diff_role_tables,
    fetch_role_table,
    load_role_table,
)
from gd_ai_assistant.server import SidecarServer

#: Changed ids printed in full; the rest are counted.
SHOW_CHANGES = 40


async def routine(server: SidecarServer) -> None:
    def progress(done: int, total: int) -> None:
        print(f"\r  described {done}/{total}", end="", file=sys.stderr, flush=True)

    path: Path = _default_path()
    previous = load_role_table(str(path))

    version = server.gd_version or "unknown"
    table, failed = await fetch_role_table(server, game_version=version, progress=progress)
    print(file=sys.stderr)

    data = table.to_json()
    data["failed"] = sorted(failed)
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

    report_diff(previous, table)


def report_diff(previous: RoleTable, table: RoleTable) -> None:
    if len(previous) == 0:
        print("\nno previous table to compare with")
        return
    diff = diff_role_tables(previous, table)
    print(f"\nchanges since the previous table (GD {previous.game_version} -> "
          f"GD {table.game_version}):")
    if diff.empty:
        print("  none")
        return
    print(f"  added {len(diff.added)}: {diff.added[:SHOW_CHANGES]}")
    print(f"  removed {len(diff.removed)}: {diff.removed[:SHOW_CHANGES]}")
    print(f"  changed {len(diff.changed)}:")
    for object_id, name, old, new in diff.changed[:SHOW_CHANGES]:
        print(f"    id {object_id} {name}: {old} -> {new}")
    if len(diff.changed) > SHOW_CHANGES:
        print(f"    ... and {len(diff.changed) - SHOW_CHANGES} more")
    print("  Review these before committing; `git diff` on roles.json has the full detail.")


if __name__ == "__main__":
    run(routine)
