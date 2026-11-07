"""Measured constants and their provenance (LEVEL-DESIGN-SPEC.md P3, 6.3).

Every value carries where it came from and how sure we are. Code reads values through
`Measured`, so a caller can always tell a calibrated number from a starting guess, and
a missing value is `None` -- never a plausible default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

Confidence = Literal["measured", "derived", "guess"]
_CONFIDENCES: tuple[str, ...] = ("measured", "derived", "guess")


@dataclass(frozen=True)
class Measured:
    """One constant: its value, where it came from, and how far to trust it."""

    value: float | None
    source: str
    confidence: Confidence

    @property
    def known(self) -> bool:
        return self.value is not None

    @property
    def calibrated(self) -> bool:
        """True only for a value measured or derived from measurement."""
        return self.value is not None and self.confidence != "guess"

    @classmethod
    def from_json(cls, raw: Any, *, where: str) -> Measured:
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: expected an object with value/source/confidence")
        value = raw.get("value")
        if value is not None and not isinstance(value, int | float):
            raise ValueError(f"{where}: value must be a number or null")
        confidence = raw.get("confidence")
        if confidence not in _CONFIDENCES:
            raise ValueError(f"{where}: confidence must be one of {_CONFIDENCES}")
        source = raw.get("source")
        if not isinstance(source, str) or not source:
            raise ValueError(f"{where}: source is required")
        return cls(
            value=None if value is None else float(value),
            source=source,
            confidence=confidence,
        )


SPEEDS: tuple[str, ...] = ("0.5x", "1x", "2x", "3x", "4x")


@dataclass(frozen=True)
class PhysicsConstants:
    """The subset of `physics_constants.json` the design layer reads so far."""

    game_version: str
    tick_rate: Measured
    ground_top_y: Measured
    units_per_second: dict[str, Measured]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> PhysicsConstants:
        if raw.get("schema") != 1:
            raise ValueError("physics_constants: unsupported schema version")
        speeds_raw = raw.get("units_per_second") or {}
        speeds = {
            speed: Measured.from_json(speeds_raw.get(speed), where=f"units_per_second.{speed}")
            for speed in SPEEDS
        }
        return cls(
            game_version=str(raw.get("game_version", "")),
            tick_rate=Measured.from_json(raw.get("tick_rate"), where="tick_rate"),
            ground_top_y=Measured.from_json(raw.get("ground_top_y"), where="ground_top_y"),
            units_per_second=speeds,
        )


def _default_path() -> Path:
    return Path(str(resources.files("gd_ai_assistant") / "data" / "physics_constants.json"))


@lru_cache(maxsize=4)
def load_constants(path: str | None = None) -> PhysicsConstants:
    """Load the constants file; the packaged one unless a path is given."""
    target = Path(path) if path is not None else _default_path()
    return PhysicsConstants.from_json(json.loads(target.read_text(encoding="utf-8")))
