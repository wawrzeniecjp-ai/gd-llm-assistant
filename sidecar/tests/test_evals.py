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


def test_mode_metrics_read_portals_exit_mode_and_holds() -> None:
    """M5 metrics: what each created portal switches to, the mode at the section's end,
    the route's holds, and a reply that says the check could not be made."""
    from evals.cases import Case
    from evals.metrics import measure
    from gd_ai_assistant.agent import TurnResult

    case = Case(id="m5", fixture="empty", prompts=["p"], section=(0.0, 1500.0))
    editor = editor_for(case)
    for type_id, col, row in ((13, 10, 1), (8, 20, 0), (101, 40, 1)):
        editor.add(type_id, 30.0 * col + 15, 30.0 * row + 15)
    metrics = measure(case, editor, {}, [TurnResult(status="completed",
                                                    text="It couldn't be verified.")],
                      [], 1.0)
    assert metrics["portals_created"] == {"ship": 1, "mini": 1}
    assert metrics["playable"] == "possible" and metrics["exit_mode"] == "ship"
    assert metrics["holds"] >= 1 and metrics["says_unverified"] is True
    assert check(metrics, "portals_created.wave", {"max": 0})[0] == "pass"


def test_m5_recordings_replay_with_their_baseline_results(tmp_path) -> None:
    """An M5 run (deepseek-flash, 2026-11-17): a mini ball section built and verified.
    (`explain-robot` was retired on 2026-11-27: the simulator plays the robot now, so the
    case asked about something no longer unverifiable; `explain-green-orb` replaces it.)"""
    runner.main(["--replay", str(runner.ROOT / "recordings"),
                 "--cases", "ball-mini", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    runs = {r["case"]: r for r in report["runs"]}
    ball = runs["ball-mini"]["metrics"]
    assert ball["playable"] == "possible" and ball["exit_mode"] == "cube"
    assert ball["portals_created"]["ball"] == 1 and ball["portals_created"]["mini"] == 1


def test_the_green_orb_fixture_is_unverifiable() -> None:
    """`explain-green-orb` asks about a part the simulator cannot check: it must stay so,
    or the case tests something untrue (as `explain-robot` came to)."""
    from gd_ai_assistant.design.constants import load_constants
    from gd_ai_assistant.design.roles import load_role_table
    from gd_ai_assistant.design.sim.cube import CubeParams
    from gd_ai_assistant.design.sim.verify import verify
    from gd_ai_assistant.design.sim.world import world_from_objects

    fixture = json.loads((runner.ROOT / "fixtures" / "levels" / "green-orb-1x.json")
                         .read_text(encoding="utf-8"))
    world = world_from_objects(fixture["objects"], load_role_table())
    result = verify(world, CubeParams.from_constants(load_constants()),
                    {"x_range": [300, 1350]})
    assert result["result"] == "unverifiable"



def test_sim3_recordings_replay_with_their_baseline_results(tmp_path) -> None:
    """Two Sim-3 runs (deepseek-flash, 2026-11-27): a spider section built and verified,
    and a green orb the model declined to call beatable. (`robot-steps` and `swing-easy`
    replay too, but take 17 and 93 s.)"""
    runner.main(["--replay", str(runner.ROOT / "recordings"),
                 "--cases", "spider-roof,explain-green-orb", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    runs = {r["case"]: r for r in report["runs"]}
    spider = runs["spider-roof"]["metrics"]
    assert spider["playable"] == "possible" and spider["exit_mode"] == "cube"
    assert spider["portals_created"]["spider"] == 1
    green = runs["explain-green-orb"]["metrics"]
    assert green["edited"] is False and green["says_unverified"] is True
