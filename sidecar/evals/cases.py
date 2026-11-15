"""Evaluation cases and level fixtures (LEVEL-DESIGN-SPEC.md 15.3, 15.4).

Cases are TOML (`cases/<id>.toml`), read with the standard library; fixtures are JSON
(`fixtures/levels/<name>.json`): `{description, settings, colors, objects}`, each object
`{id, x, y, ...named properties}` as `FakeEditor.add` takes them.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fake_editor import FakeEditor

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "cases"
FIXTURES = ROOT / "fixtures" / "levels"


@dataclass
class Case:
    id: str
    fixture: str
    prompts: list[str]
    #: [x0, x1] in level units: what `playable` and the route metrics are measured over.
    section: tuple[float, float] | None
    max_tool_rounds: int = 40
    hard: dict[str, Any] = field(default_factory=dict)
    soft: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    #: A level plan to start from (its `music`, `concept`, ...), or None for no plan.
    plan: dict[str, Any] | None = None


def load_case(path: Path) -> Case:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    prompts = data.get("prompts") or [data["prompt"]]
    section = data.get("section")
    asserts = data.get("assert") or {}
    return Case(
        id=data.get("id", path.stem),
        fixture=data.get("fixture", "empty"),
        prompts=[str(p).strip() for p in prompts],
        section=(float(section[0]), float(section[1])) if section else None,
        max_tool_rounds=int((data.get("budget") or {}).get("max_tool_rounds", 40)),
        hard=dict(asserts.get("hard") or {}),
        soft=dict(asserts.get("soft") or {}),
        description=data.get("description", ""),
        plan=data.get("plan"),
    )


def load_cases(selection: str = "all") -> list[Case]:
    cases = [load_case(p) for p in sorted(CASES.glob("*.toml"))]
    if selection == "all":
        return cases
    wanted = {s.strip() for s in selection.split(",") if s.strip()}
    unknown = wanted - {c.id for c in cases}
    if unknown:
        raise SystemExit(f"unknown case(s): {', '.join(sorted(unknown))}")
    return [c for c in cases if c.id in wanted]


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def editor_for(case: Case) -> FakeEditor:
    """A fake editor holding the case's fixture, as the user's level."""
    fixture = load_fixture(case.fixture)
    editor = FakeEditor(level_name=f"eval {case.id}")
    editor.unsupported_requests = set()  # what the server provides per connection
    editor.settings.update(fixture.get("settings") or {})
    for channel, rgb in (fixture.get("colors") or {}).items():
        editor.colors[int(channel)] = tuple(rgb)
    for obj in fixture.get("objects") or []:
        obj = dict(obj)
        editor.add(obj.pop("id"), obj.pop("x"), obj.pop("y"), **obj)
    return editor
