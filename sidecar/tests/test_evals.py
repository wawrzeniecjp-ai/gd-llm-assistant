"""The evaluation harness itself, with recorded transcripts instead of a model (15.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import runner
from evals.cases import editor_for, load_cases
from evals.metrics import check
from gd_ai_assistant.capture import describe_capture


def test_every_case_loads_with_a_fixture_and_assertions() -> None:
    cases = load_cases()
    assert len(cases) >= 12
    for case in cases:
        editor_for(case)                       # the fixture exists and loads
        assert case.prompts and case.hard, case.id
        if "playable" in case.hard:
            # An empty level is already playable: a build case must also require an edit.
            assert case.hard.get("edited") is True, case.id


def test_replayed_recordings_pass_and_write_a_report(tmp_path) -> None:
    code = runner.main(["--replay", str(runner.ROOT / "recordings"),
                        "--cases", "fix-wall,explain-wall",
                        "--out", str(tmp_path)])
    assert code == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    fixed = next(r for r in report["runs"] if r["case"] == "fix-wall")
    assert fixed["metrics"]["playable"] == "possible"
    assert fixed["metrics"]["objects_deleted"] == 3
    assert (tmp_path / "fix-wall-1" / "section.png").exists()
    assert "fix-wall" in (tmp_path / "report.html").read_text(encoding="utf-8")


def test_a_case_plan_is_seeded_and_beat_alignment_measured(tmp_path) -> None:
    """The on-beat case brings a 120 bpm plan. The recording (deepseek-flash, a real run)
    reads the plan, uses beat_grid and places five spikes whose press windows are centred
    on beats -- though the solver's own press in each sits ~50 ms late, which the metric
    must not count against it."""
    runner.main(["--replay", str(runner.ROOT / "recordings"), "--cases", "cube-on-beat",
                 "--out", str(tmp_path)])
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    metrics = report["runs"][0]["metrics"]
    assert metrics["playable"] == "possible" and metrics["used_beat_grid"]
    assert metrics["beat_alignment"] == 1.0 and len(metrics["beat_offsets_ms"]) == 5
    assert all(abs(o) < 1 for o in metrics["beat_offsets_ms"])
    assert report["runs"][0]["verdicts"]["soft:beat_alignment"]["outcome"] == "pass"


def test_a_failed_hard_assertion_fails_the_run(tmp_path) -> None:
    """The fix-wall recording replayed as if it were the read-only question: it edits."""
    recordings = tmp_path / "rec"
    recordings.mkdir()
    (recordings / "explain-wall.json").write_text(
        (runner.ROOT / "recordings" / "fix-wall.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    code = runner.main(["--replay", str(recordings), "--cases", "explain-wall",
                        "--out", str(tmp_path / "out")])
    assert code == 1


@pytest.mark.parametrize(("value", "condition", "outcome"), [
    (5, {"between": [1, 10]}, "pass"),
    (11, {"between": [1, 10]}, "fail"),
    (3, {"min": 4}, "fail"),
    (3, {"max": 4}, "pass"),
    ("possible", "possible", "pass"),
    (True, {"min": 0}, "fail"),            # a flag is not a number
])
def test_assertions(value, condition, outcome) -> None:
    assert check({"m": value}, "m", condition)[0] == outcome


def test_an_unmeasured_metric_is_not_a_pass() -> None:
    assert check({}, "playable", "possible") == ("n/a", None)
    assert check({"created_roles": {}}, "created_roles.orb", {"min": 1}) == ("fail", 0)


async def test_the_fake_editor_screenshot_is_a_labelled_schematic() -> None:
    editor = editor_for(next(c for c in load_cases() if c.fixture == "wall-1x"))
    await editor.request("set_camera", {"x": 900, "y": 140, "zoom": 1.0})
    response = await editor.request("capture_screenshot", {"region": "screen"})
    assert response.ok
    assert Path(response.result["path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "schematic" in describe_capture(response.result)["kind"]
