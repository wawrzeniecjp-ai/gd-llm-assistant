"""Protocol schema loading and validation (SPEC.md 7, 13).

The protocol is the test seam: a Geode mod cannot run headless in CI, so both
sides validate frames against the JSON Schemas in `protocol/v1/` instead.

`envelope.schema.json` is the single entry point. Its `$defs.registry` maps a
frame's `type` to the schema for its payload and the direction it may travel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

PROTOCOL_VERSION = 1

Direction = Literal["m2s", "s2m", "both"]


def protocol_dir(version: int = PROTOCOL_VERSION) -> Path:
    """Locate `protocol/` relative to the repository root.

    The sidecar lives at `<repo>/sidecar/`, so the schemas are two levels up.
    """
    return Path(__file__).resolve().parents[3] / "protocol" / f"v{version}"


@dataclass(frozen=True)
class MessageSpec:
    """One entry of the envelope registry."""

    type: str
    direction: Direction
    payload_ref: str
    result_ref: str | None = None

    @property
    def is_request(self) -> bool:
        return self.result_ref is not None


@lru_cache(maxsize=4)
def _load(
    version: int = PROTOCOL_VERSION,
) -> tuple[dict[str, Any], dict[str, Any], Registry, Draft202012Validator]:
    """Load every schema for a protocol version once, and cache it.

    Returns the envelope document, a filename/$id -> document map, a registry
    usable by any payload validator, and a prebuilt envelope validator.
    """
    directory = protocol_dir(version)
    if not directory.is_dir():
        raise FileNotFoundError(f"protocol schemas not found at {directory}")

    registry = Registry()
    documents: dict[str, dict[str, Any]] = {}
    envelope: dict[str, Any] | None = None

    for path in sorted(directory.glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resource = DRAFT202012.create_resource(contents)

        # Keyed by filename so payload refs ("hello.schema.json#/$defs/x") and
        # relative cross-file refs ("common.schema.json#/$defs/x") resolve
        # without absolute-URI resolution, and by $id for absolute refs.
        documents[path.name] = contents
        registry = registry.with_resource(path.name, resource)

        uri = contents.get("$id", path.name)
        documents[uri] = contents
        if uri != path.name:
            registry = registry.with_resource(uri, resource)

        if path.name == "envelope.schema.json":
            envelope = contents

    if envelope is None:
        raise FileNotFoundError(f"envelope.schema.json missing from {directory}")

    return envelope, documents, registry, Draft202012Validator(envelope, registry=registry)


def registry_entries(version: int = PROTOCOL_VERSION) -> dict[str, MessageSpec]:
    """Every message type the protocol defines, keyed by `type`."""
    envelope, _, _, _ = _load(version)
    raw = envelope["$defs"]["registry"]["properties"]
    specs: dict[str, MessageSpec] = {}
    for type_name, entry in raw.items():
        # Each registry entry is a small JSON Schema; its keys live under
        # `properties`, each holding a `const`.
        fields = entry["properties"]
        specs[type_name] = MessageSpec(
            type=type_name,
            direction=fields["direction"]["const"],
            payload_ref=fields["payload"]["const"],
            result_ref=fields.get("result", {}).get("const"),
        )
    return specs


def spec_for(type_name: str, version: int = PROTOCOL_VERSION) -> MessageSpec | None:
    return registry_entries(version).get(type_name)


def validate_envelope(frame: Any, version: int = PROTOCOL_VERSION) -> list[str]:
    """Validate the envelope shape (type, id, payload present).

    Returns a list of human-readable errors; empty means valid. Payload contents
    are validated separately by :func:`validate_payload`, because the envelope
    schema intentionally treats `payload` as an opaque object.
    """
    _, _, _, validator = _load(version)
    return [
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
        for e in validator.iter_errors(frame)
    ]


def validate_payload(frame: dict[str, Any], version: int = PROTOCOL_VERSION) -> list[str]:
    """Validate a frame's payload against the schema registered for its `type`.

    The payload schema is *referenced* rather than inlined, so local refs inside
    it (for example `#/$defs/ResponseOk` within common.schema.json) still resolve
    against the document they were written in.
    """
    type_name = frame.get("type")
    if not isinstance(type_name, str):
        return ["type: missing or not a string"]

    spec = spec_for(type_name, version)
    if spec is None:
        return [f"type: unknown message type {type_name!r}"]

    _, _, registry, _ = _load(version)
    validator = Draft202012Validator({"$ref": spec.payload_ref}, registry=registry)
    return [
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
        for e in validator.iter_errors(frame.get("payload", {}))
    ]


def validate_frame(frame: dict[str, Any], version: int = PROTOCOL_VERSION) -> list[str]:
    """Envelope and payload validation in one call."""
    return validate_envelope(frame, version) or validate_payload(frame, version)
