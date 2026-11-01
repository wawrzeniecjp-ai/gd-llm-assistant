"""Reading a capture back off disk (SPEC.md 7.5.6).

The rendering happens in the game; everything here is the part that can be wrong quietly.
Three of these cases would fail silently in a way the model would then paper over:

- A capture file that is missing or unreadable must produce an error, not an empty image.
  A model handed nothing will still describe the level from memory, which is the one
  outcome this feature exists to prevent.
- A *tiny* file is almost certainly a blank PNG -- the game wrote something, but not a
  picture of anything. `MIN_USEFUL_BYTES` rejects it rather than sending mush.
- A huge file means the request was wrong (a whole level at high zoom), not that the level
  is detailed, so it is refused with advice rather than sent and rejected by the provider.
"""

from __future__ import annotations

import base64

from gd_ai_assistant import capture


def test_a_missing_path_is_an_error_not_an_empty_image() -> None:
    url, info = capture.load_capture("")
    assert url is None
    assert "error" in info


def test_a_file_that_does_not_exist_names_the_problem(tmp_path) -> None:
    url, info = capture.load_capture(str(tmp_path / "nothing-here.png"))
    assert url is None
    assert "could not read" in info["error"]


def test_a_blank_capture_is_refused_rather_than_sent(tmp_path) -> None:
    """A few bytes is the game writing an empty PNG: sending it would hand the model mush
    and invite it to describe a level it cannot see."""
    tiny = tmp_path / "blank.png"
    tiny.write_bytes(b"\x89PNG" + b"\x00" * 10)

    url, info = capture.load_capture(str(tiny))
    assert url is None
    assert "probably blank" in info["error"]


def test_an_oversized_capture_is_refused_with_advice(tmp_path) -> None:
    """The advice matters: "too big" alone gives the model nothing to do differently."""
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG" + b"\x00" * 4096)

    url, info = capture.load_capture(str(big), max_bytes=1024)
    assert url is None
    assert "smaller region" in info["error"]


def test_a_real_capture_becomes_a_png_data_url(tmp_path) -> None:
    """The shape the provider expects. `data:` with a PNG mime and base64 payload."""
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024
    file = tmp_path / "capture.png"
    file.write_bytes(payload)

    url, info = capture.load_capture(str(file))
    assert url is not None
    assert url.startswith("data:image/png;base64,")
    assert info["bytes"] == len(payload)

    # The payload must round-trip: a truncated or double-encoded image would reach the
    # provider as garbage and be rejected there, far from the cause.
    encoded = url.split(",", 1)[1]
    assert base64.b64decode(encoded) == payload


def test_a_selection_capture_says_it_is_a_detail_view() -> None:
    """The model needs to know whether it is looking at the level or at a corner of it,
    or it will reason about framing as though it were the whole picture."""
    described = capture.describe_capture(
        {"width": 300, "height": 200, "targets": 3, "region": {"x": 0, "y": 0, "w": 300, "h": 200}}
    )
    assert described["targets"] == 3
    assert "detail view" in described["region_note"]


def test_a_whole_screen_capture_says_so() -> None:
    described = capture.describe_capture({"width": 512, "height": 320, "targets": 0})
    assert "whole editor view" in described["region_note"]
