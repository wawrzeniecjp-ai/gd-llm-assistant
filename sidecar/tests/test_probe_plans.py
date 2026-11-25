"""Calibration probe plans, checked without the game (LEVEL-DESIGN-SPEC.md 7.4).

A probe run costs minutes of the user's time in the editor, so its layouts and aims are
checked here first: everything inside the area the script checks is empty, every input
inside its run, and every press meant to meet an object timed to meet it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
calibrate = pytest.importorskip("calibrate")


def test_p19_robot_plan_is_sound() -> None:
    plan = calibrate.p19_plan()
    labels = [label for label, *_ in plan]
    assert len(labels) == len(set(labels)), "labels name the runs in the report"
    assert 40 <= len(plan) <= 80                          # a few minutes in the game
    parts = {meta["part"] for *_, meta in plan}
    assert parts == {"A", "B", "C", "D", "E", "F"}
    for label, objects, inputs, max_ticks, meta in plan:
        # The script only checks that the first CLEAR_X units are empty.
        assert all(o["x"] < calibrate.CLEAR_X for o in objects), label
        assert sum(o["id"] == calibrate.ROBOT_PORTAL for o in objects) == 1, label
        ticks = [i["tick"] for i in inputs]
        assert ticks == sorted(ticks) and all(0 < t < max_ticks for t in ticks), label
        if "touch" in meta and "press_tick" in meta:
            first, last = meta["touch"]
            assert first <= meta["press_tick"] <= last, label


def test_p19_presses_after_the_robot_portal() -> None:
    """A press before the portal would be a cube jump, measuring nothing."""
    portal = calibrate.cell(6, 0)[0]
    flat = calibrate._p19_base()
    at_portal = calibrate._sim_tick_at(calibrate._cube_only(flat), [], portal + 17)
    assert at_portal is not None
    for label, _objects, inputs, _max, meta in calibrate.p19_plan():
        if inputs and not meta.get("high"):
            assert inputs[0]["tick"] > at_portal + 20, label


class _Response:
    def __init__(self, result: dict) -> None:
        self.ok = True
        self.error = None
        self.result = result


class _FakeGame:
    """Answers the requests a probe makes, with an empty trace for every replay."""

    def __init__(self) -> None:
        self.replays = 0

    async def request(self, type_name: str, payload: dict, timeout: float = 0.0):
        if type_name == "begin_transaction":
            return _Response({"txn_id": "t"})
        if type_name == "create_objects":
            return _Response({"created": list(range(len(payload["objects"])))})
        if type_name == "run_replay":
            self.replays += 1
            return _Response({"outcome": "max_ticks", "ticks": payload["max_ticks"],
                              "trace": {"tick": [], "x": [], "y": [], "vy": [],
                                        "grounded": [], "mode": [], "gravity": [],
                                        "mini": []}})
        return _Response({})


def test_p19_runs_end_to_end_against_a_fake_game(tmp_path, monkeypatch) -> None:
    """Every run goes through `_record` and the fixture is saved: a meta key that
    clashes with `_record`'s own arguments failed on run 12 in the game (2026-11-24)."""
    import asyncio

    monkeypatch.setattr(calibrate, "FIXTURES", tmp_path / "fixtures")
    monkeypatch.setattr(calibrate, "RAW", tmp_path / "raw")
    game = _FakeGame()
    out: dict = {}
    asyncio.run(calibrate.probe_robot(calibrate.Runner(game), out))
    assert game.replays == len(calibrate.p19_plan()) == len(out["P19_robot"])
    assert (tmp_path / "fixtures" / "cal_robot.json").exists()
    assert (tmp_path / "raw" / "P19.json").exists()


def test_p19_keeps_the_runs_made_before_a_failure(tmp_path, monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(calibrate, "FIXTURES", tmp_path / "fixtures")
    monkeypatch.setattr(calibrate, "RAW", tmp_path / "raw")
    game = _FakeGame()
    real = game.request

    async def fail_late(type_name: str, payload: dict, timeout: float = 0.0):
        if type_name == "run_replay" and game.replays == 5:
            raise RuntimeError("the game went away")
        return await real(type_name, payload, timeout)

    game.request = fail_late  # type: ignore[method-assign]
    out: dict = {}
    with pytest.raises(RuntimeError):
        asyncio.run(calibrate.probe_robot(calibrate.Runner(game), out))
    assert len(out["P19_robot"]) == 5
    assert (tmp_path / "fixtures" / "cal_robot.json").exists()


def test_p20_robot_contacts_plan_is_sound() -> None:
    plan = calibrate.p20_plan()
    labels = [label for label, *_ in plan]
    assert len(labels) == len(set(labels))
    assert 50 <= len(plan) <= 100
    assert {meta["part"] for *_, meta in plan} == {"A", "B", "C", "D", "E"}
    for label, objects, inputs, max_ticks, _meta in plan:
        assert all(o["x"] < calibrate.CLEAR_X for o in objects), label
        assert sum(o["id"] == calibrate.ROBOT_PORTAL for o in objects) == 1, label
        ticks = [i["tick"] for i in inputs]
        assert ticks == sorted(ticks) and all(0 < t < max_ticks for t in ticks), label
    # Upside down, the robot has a roof to land on this time (P19's had none).
    for label, objects, *_rest, meta in plan:
        if meta["part"] == "A":
            roof = [o for o in objects if o["id"] == 1 and o["y"] > 200]
            assert roof, label


def test_p20_sweeps_straddle_the_simulators_edges() -> None:
    """C and E are aimed where the simulator changes its answer: each sweep holds both
    outcomes, so the game decides which side of every edge is right."""
    by_sweep: dict[tuple, set[str]] = {}
    for _label, objects, inputs, _max, meta in calibrate.p20_plan():
        if meta["part"] in ("C", "E"):
            key = (meta["part"], meta["hold"])
            by_sweep.setdefault(key, set()).add(calibrate._robot_outcome(objects, inputs))
    assert by_sweep
    for key, outcomes in by_sweep.items():
        if key[0] == "C":
            assert outcomes == {"death", "max_ticks"}, key
    assert set().union(*(o for k, o in by_sweep.items() if k[0] == "E")) == {
        "death", "max_ticks"}


def test_p20_runs_end_to_end_against_a_fake_game(tmp_path, monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(calibrate, "FIXTURES", tmp_path / "fixtures")
    monkeypatch.setattr(calibrate, "RAW", tmp_path / "raw")
    game = _FakeGame()
    out: dict = {}
    asyncio.run(calibrate.probe_robot_contacts(calibrate.Runner(game), out))
    assert game.replays == len(calibrate.p20_plan()) == len(out["P20_robot"])
    assert (tmp_path / "fixtures" / "cal_robot_contacts.json").exists()


def test_p21_spider_plan_is_sound() -> None:
    plan = calibrate.p21_plan()
    labels = [label for label, *_ in plan]
    assert len(labels) == len(set(labels))
    assert 25 <= len(plan) <= 60
    assert {meta["part"] for *_, meta in plan} == {"A", "B", "C", "D", "E", "F"}
    for label, objects, inputs, max_ticks, meta in plan:
        assert all(o["x"] < calibrate.CLEAR_X for o in objects), label
        assert sum(o["id"] == calibrate.SPIDER_PORTAL for o in objects) == 1, label
        assert not any(o["id"] == calibrate.ROBOT_PORTAL for o in objects), label
        ticks = [i["tick"] for i in inputs]
        assert ticks == sorted(ticks) and all(0 < t < max_ticks for t in ticks), label
        # A spider pressed with nothing above falls off the level (P19's robots did):
        # every run has a roof, but the one meant to show that.
        has_roof = any(o["id"] == 1 and o["y"] > 60 for o in objects)
        assert has_roof == (meta["part"] != "B"), label
        if "touch" in meta and "press_tick" in meta:
            assert meta["touch"][0] <= meta["press_tick"] <= meta["touch"][1], label


def test_p21_runs_end_to_end_against_a_fake_game(tmp_path, monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(calibrate, "FIXTURES", tmp_path / "fixtures")
    monkeypatch.setattr(calibrate, "RAW", tmp_path / "raw")
    game = _FakeGame()
    out: dict = {}
    asyncio.run(calibrate.probe_spider(calibrate.Runner(game), out))
    assert game.replays == len(calibrate.p21_plan()) == len(out["P21_spider"])
    assert (tmp_path / "fixtures" / "cal_spider.json").exists()


def test_p22_spider_contacts_plan_is_sound() -> None:
    plan = calibrate.p22_plan()
    labels = [label for label, *_ in plan]
    assert len(labels) == len(set(labels))
    assert 25 <= len(plan) <= 60
    assert {meta["part"] for *_, meta in plan} == {"A", "B", "C", "D", "E"}
    for label, objects, inputs, max_ticks, _meta in plan:
        assert all(o["x"] < calibrate.CLEAR_X for o in objects), label
        assert sum(o["id"] == calibrate.SPIDER_PORTAL for o in objects) == 1, label
        ticks = [i["tick"] for i in inputs]
        assert ticks == sorted(ticks) and all(0 < t < max_ticks for t in ticks), label


def test_p22_teleport_sweeps_straddle_the_simulators_edges() -> None:
    """The leaving and stepped B sweeps hold both of the simulator's answers for where
    the first press lands. The entering one did too when P22 was written; the game then
    found the block on every entering run, and the simulator does now (P22 results)."""
    landed: dict[tuple, set[float]] = {}
    for _label, objects, inputs, max_ticks, meta in calibrate.p22_plan():
        if meta["part"] != "B":
            continue
        trace = calibrate._sim_run(objects, inputs, max_ticks).trace
        after = trace["y"][trace["tick"].index(inputs[0]["tick"] + 1)]
        landed.setdefault((meta.get("side"), meta.get("kind")), set()).add(round(after, 1))
    assert len(landed) == 3
    assert landed[("entering", None)] == {196.5}
    assert len(landed[("leaving", None)]) == len(landed[(None, "stepped")]) == 2, landed


def test_p22_high_portals_are_reached() -> None:
    """D's portals sit where the pad's flight crosses them, so the band is raised."""
    floors = set()
    for _label, objects, inputs, max_ticks, meta in calibrate.p22_plan():
        if meta["part"] == "D":
            final = calibrate._sim_run(objects, inputs, max_ticks).final
            assert final.mode == "spider" and final.bounds is not None
            floors.add(final.bounds[0])
    assert floors == {30.0, 60.0}


def test_p22_runs_end_to_end_against_a_fake_game(tmp_path, monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(calibrate, "FIXTURES", tmp_path / "fixtures")
    monkeypatch.setattr(calibrate, "RAW", tmp_path / "raw")
    game = _FakeGame()
    out: dict = {}
    asyncio.run(calibrate.probe_spider_contacts(calibrate.Runner(game), out))
    assert game.replays == len(calibrate.p22_plan()) == len(out["P22_spider"])
    assert (tmp_path / "fixtures" / "cal_spider_contacts.json").exists()
