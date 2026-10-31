#!/usr/bin/env python3
"""Generate the save-string property table from the Geode bindings (SPEC.md 7.5.3).

The bindings are the community's reverse-engineered layout of the game's classes, and
they carry one thing we cannot otherwise get: **members are annotated with the
save-string key they are serialised under**,

    // property 128
    float m_scaleX;

so the mapping "key 128 means scale_x" is readable straight out of the game's own
structure rather than guessed or probed one key at a time. 519 members are annotated
in the 2.2091 bindings.

Why a generated file rather than code: the table is large, changes only when the game
does, and is data rather than logic. Regenerating is one command:

    python scripts/gen_properties.py \\
        ../build/_deps/bindings-src/bindings/2.2091/GeometryDash.bro

The output is committed, so the sidecar has no build-time dependency on the bindings
being present. `gdVersion` records which bindings produced it.

Format of the emitted JSON:

    {
      "gdVersion": "2.2091",
      "source": "GeometryDash.bro",
      "classes": {
        "RingObject": {
          "parent": "EffectGameObject",
          "props": { "151": ["m_objectID", "int"], ... }
        }
      },
      "keyNames": { "128": "m_scaleX" }   // global reverse map, see below
    }

The `keyNames` map exists because the save-string vocabulary is **global and
additive**: a key means the same thing on every object that has it. That makes it
directly useful for reading an object's save string without knowing its class -- which
is the common case, because the mod does not report classes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

#: The root of the object hierarchy. Only classes descended from it are emitted:
#: everything else in the bindings (layer classes, popups, delegates) has no save
#: string and would only bloat the table.
ROOT_CLASS = "GameObject"

_CLASS_RE = re.compile(r"^class\s+([\w:]+)\s*(?::\s*([\w:]+))?")
#: Detects that a line is an annotation, and
#: extracts **every** key on it. Two patterns, not one, and the distinction is a bug
#: that cost an afternoon: the annotation is `// property 28, property 29`, so requiring
#: `//` before each key matches only the first. Every multi-key member (all the
#: `CCPoint` pairs: position, move offset, scale) silently lost its second half, which
#: is why x and y were missing from a table that had z-layer and groups.
_ANNOTATION_RE = re.compile(r"//\s*property\s+\d+", re.IGNORECASE)
_KEY_RE = re.compile(r"property\s+(\d+)", re.IGNORECASE)
_DECL_RE = re.compile(r"^\s*(.+?)\s*\*?\s*\bm_(\w+)\s*;")
#: Members named `m_property310` are the bindings' placeholders for fields whose real
#: name was not recovered. Emitting them would tell the model "key 310 is called
#: m_property310", which is worse than saying nothing.
_PLACEHOLDER_RE = re.compile(r"^property\d+$")


def value_type(cpp_type: str) -> str:
    """How a value is written in a save string, judged from the member's C++ type."""
    clean = re.sub(r"\b(const|unsigned|class|struct)\b", "", cpp_type).strip()
    if clean == "bool":
        return "bool"
    if re.fullmatch(r"(int|short|char|long)", clean):
        return "int"
    if re.fullmatch(r"(float|double)", clean):
        return "float"
    if clean in {"gd::string", "std::string"}:
        return "string"
    # Enums are plain identifiers and are saved as their integer value.
    if re.fullmatch(r"[A-Z]\w*", clean):
        return "int"
    # Colours, HSV, vectors and the like have their own text encodings.
    return "raw"


def parse(lines: list[str]) -> tuple[dict[str, str | None], dict[str, dict[str, list[str]]]]:
    parents: dict[str, str | None] = {}
    props: dict[str, dict[str, list[str]]] = {}
    current: str | None = None

    for index, line in enumerate(lines):
        match = _CLASS_RE.match(line)
        if match:
            current = match.group(1)
            parents[current] = match.group(2)
            continue

        if current is None or not _ANNOTATION_RE.search(line):
            continue

        keys = _KEY_RE.findall(line)

        # The annotated member is the next line that is neither blank nor a comment.
        probe = index + 1
        while probe < len(lines) and (
            not lines[probe].strip() or lines[probe].lstrip().startswith("//")
        ):
            probe += 1
        if probe >= len(lines):
            continue

        decl = _DECL_RE.match(lines[probe])
        if not decl:
            continue
        cpp_type, member = decl.group(1), decl.group(2)
        if _PLACEHOLDER_RE.match(member):
            continue

        for position, key in enumerate(keys):
            # "property 28, property 29" above a CCPoint: first key is x, second is y.
            # Anything beyond two keys would be a guess, so it is left as the bare member
            # name rather than inventing a suffix.
            if len(keys) == 2:
                name = member + ("X" if position == 0 else "Y")
                kind = "float"
            else:
                name = member
                kind = value_type(cpp_type)
            props.setdefault(current, {})[key] = [name, kind]

    return parents, props


def descends_from_root(name: str, parents: dict[str, str | None]) -> bool:
    seen: set[str] = set()
    current: str | None = name
    while current is not None and current not in seen:
        if current == ROOT_CLASS:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


#: Keys the bindings do **not** annotate, filled from measurement instead.
#:
#: Deliberately tiny. `x` and `y` are omitted from the bindings entirely -- the members
#: (`m_positionX`/`m_positionY`) carry no `// property` comment -- yet they are the two
#: properties that matter most, and they have been measured directly and repeatedly:
#: every object save string observed in this project reads `1,<id>,2,<x>,3,<y>`, e.g.
#: `1,83,2,435,3,-105`.
#:
#: Nothing else is added. Keys 4-9 are unannotated too, and although a colour trigger's
#: save string shows `7,255,8,255,9,255` for its RGB, no measurement here distinguishes
#: what 4, 5 and 6 mean, so they are left out rather than guessed. A missing key makes
#: the model say "I do not know"; a wrong key makes it confidently edit the wrong thing.
_SUPPLEMENT: dict[str, list[str]] = {
    "2": ["m_positionX", "float"],
    "3": ["m_positionY", "float"],
}

#: Where each supplemented key came from, emitted so the file never looks hand-authored.
_SUPPLEMENT_SOURCE = "measured in-editor (every observed save string is 1,<id>,2,<x>,3,<y>)"


def build(bro_path: Path) -> dict:
    lines = bro_path.read_text(encoding="utf-8", errors="replace").splitlines()
    parents, props = parse(lines)

    classes: dict[str, dict] = {}
    for name in sorted(parents):
        if not descends_from_root(name, parents):
            continue
        parent = parents[name]
        classes[name] = {
            "parent": None if name == ROOT_CLASS else parent,
            "props": props.get(name, {}),
        }

    # Supplement the root class, so every descendant inherits it when the chain is
    # resolved. Not merged into every class: one copy is enough and it keeps the
    # provenance of the supplemented keys visible.
    if ROOT_CLASS in classes:
        for key, value in _SUPPLEMENT.items():
            classes[ROOT_CLASS]["props"].setdefault(key, value)

    # Global reverse map: key -> member name. The save-string vocabulary is global and
    # additive, which is what makes this meaningful. Where two classes disagree about a
    # key, the earliest alphabetically wins and the conflict is reported rather than
    # silently hidden.
    candidates: dict[str, list[str]] = defaultdict(list)
    for name in sorted(classes):
        for key, (member, _kind) in sorted(classes[name]["props"].items()):
            if member not in candidates[key]:
                candidates[key].append(member)

    key_names = {key: names[0] for key, names in candidates.items()}
    conflicts = {key: names for key, names in candidates.items() if len(names) > 1}

    return {
        "gdVersion": bro_path.parent.name,
        "source": bro_path.name,
        "classes": classes,
        "keyNames": dict(sorted(key_names.items(), key=lambda item: int(item[0]))),
        "conflicts": dict(sorted(conflicts.items(), key=lambda item: int(item[0]))),
        "supplement": {
            "keys": dict(sorted(_SUPPLEMENT.items(), key=lambda item: int(item[0]))),
            "source": _SUPPLEMENT_SOURCE,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bro", type=Path, help="path to bindings/<version>/GeometryDash.bro")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "src"
        / "gd_ai_assistant"
        / "data"
        / "properties.json",
        help="output path (default: the package's data directory)",
    )
    args = parser.parse_args(argv)

    if not args.bro.is_file():
        print(f"no such file: {args.bro}", file=sys.stderr)
        return 1

    table = build(args.bro)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")

    class_count = len(table["classes"])
    prop_count = sum(len(entry["props"]) for entry in table["classes"].values())
    print(
        f"wrote {class_count} classes, {prop_count} properties, "
        f"{len(table['keyNames'])} distinct keys to {args.out}",
        file=sys.stderr,
    )
    if table["conflicts"]:
        print(
            f"note: {len(table['conflicts'])} key(s) map to different names in different "
            f"classes; the first alphabetically was kept in `keyNames`",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
