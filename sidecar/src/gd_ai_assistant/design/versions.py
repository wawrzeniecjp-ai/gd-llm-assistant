"""Which game version the dumped data came from, and whether it matches (docs/UPGRADING.md).

`roles.json` and `physics_constants.json` are measured from one game version. A new GD
release can add object ids, retype old ones or change physics, and nothing would fail
loudly: the design layer would quietly judge levels with stale data. So the sidecar
compares the connected game's version (from the mod's hello) with the version each file
records, and logs a warning naming the script that refreshes it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .constants import _default_path as _constants_path
from .roles import _default_path as _roles_path

log = logging.getLogger(__name__)

#: (file, what refreshes it)
_DATA_FILES: tuple[tuple[str, str], ...] = (
    ("roles", "scripts/dump_roles.py"),
    ("physics_constants", "scripts/calibrate.py (M2); until then re-run the S-L1 check"),
)


def _recorded_version(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    version = raw.get("game_version") if isinstance(raw, dict) else None
    return version if isinstance(version, str) else None


def data_version_warnings(
    gd_version: str, paths: dict[str, Path] | None = None
) -> list[str]:
    """One message per data file whose recorded game version differs from `gd_version`.

    A missing file is not reported here: `load_role_table` already warns about that, and
    an absent file cannot be stale.
    """
    paths = paths or {"roles": _roles_path(), "physics_constants": _constants_path()}
    warnings = []
    for name, refresh in _DATA_FILES:
        path = paths.get(name)
        if path is None:
            continue
        recorded = _recorded_version(path)
        if recorded is not None and recorded != gd_version:
            warnings.append(
                f"{path.name} was measured on GD {recorded} but the game is GD {gd_version}; "
                f"refresh it with {refresh} (docs/UPGRADING.md)"
            )
    return warnings
