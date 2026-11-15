"""Turning a capture into something the model can actually look at.

The mod writes a PNG and reports its path; the work here is reading it back, deciding
whether it is worth sending, and building a `data:` URL for the provider.

Why a file and not base64 through the protocol: a frame is capped at 1 MB, a PNG is often
100-200 KB before base64 inflates it by a third, and the mod would need a base64 encoder.
Both processes are on one machine by construction, so the file is the natural medium rather
than a compromise.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

log = logging.getLogger("sidecar")

#: Refuse anything larger than this from disk. The API allows 32 MiB; a capture from a
#: 512x320 editor view is a couple of hundred kilobytes, so anything near this limit means
#: something is wrong rather than that the level is detailed.
MAX_FILE_BYTES = 8 * 1024 * 1024

#: Below this the model is being handed mush. Reported instead of sent, so it can say the
#: capture failed rather than describe a picture it cannot see.
MIN_USEFUL_BYTES = 512


def load_capture(path: str, max_bytes: int = MAX_FILE_BYTES) -> tuple[str | None, dict]:
    """Read a capture and return a `data:` URL, or an explanation of why not."""
    if not path:
        return None, {"error": "the mod reported no capture path"}

    file = Path(path)
    try:
        size = file.stat().st_size
    except OSError as exc:
        # The path is on the game's machine, and a missing file usually means the write
        # failed or the mod is on a different machine from the sidecar -- neither of which
        # the model can fix, so it is told plainly rather than retried.
        log.warning("capture: cannot stat %s: %s", path, exc)
        return None, {"error": f"could not read the capture at {path}: {exc.strerror}"}

    if size > max_bytes:
        return None, {
            "error": (
                f"the capture is {size} bytes, over the {max_bytes} limit -- request a "
                "smaller region"
            )
        }
    if size < MIN_USEFUL_BYTES:
        return None, {"error": f"the capture is only {size} bytes; it is probably blank"}

    try:
        data = file.read_bytes()
    except OSError as exc:
        return None, {"error": f"could not read the capture: {exc.strerror}"}

    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{encoded}", {"bytes": size}


def describe_capture(result: dict) -> dict:
    """The tool result the model sees, before the image is attached separately.

    Deliberately shaped so a failure is legible: if the capture could not be read, the model
    is told that and must not pretend to have seen anything.
    """
    width = result.get("width")
    height = result.get("height")
    targets = result.get("targets") or 0
    region = result.get("region") or {}
    viewport = result.get("viewport") or {}

    description = {
        "width": width,
        "height": height,
        "targets": targets,
        "region": region,
    }
    if Path(str(result.get("path") or "")).name.startswith("schematic-"):
        # The evaluation harness's fake editor draws a schematic instead of the game's
        # graphics (LEVEL-DESIGN-SPEC.md 15.2); a model must not judge looks from it.
        description["kind"] = ("a schematic: coloured boxes by role, not the game's "
                               "graphics; judge layout from it, not looks")
    if targets:
        description["region_note"] = (
            f"cropped around {targets} selected object(s), padded; this is a detail view"
        )
    else:
        description["region_note"] = "the whole editor view"

    # Which part of the level is in the picture, in the same coordinates objects use. Worth
    # spending a line on: it lets the model say "the object at x=940 is off to the right
    # edge" instead of describing pixels, and it is the only way to connect an image to a
    # query result without guessing.
    if viewport:
        description["shows_level_area"] = (
            f"x {viewport.get('x1', 0):.0f} to {viewport.get('x2', 0):.0f}, "
            f"y {viewport.get('y1', 0):.0f} to {viewport.get('y2', 0):.0f}"
        )
    return description
