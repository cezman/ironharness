"""Tests of the plant target (a closed "plant + controller" loop, see ironbench/plant.py):
unit tests of the physics/metrics + integration through the real worker (a local
process, no WSL or simulators)."""

from __future__ import annotations

import math
import textwrap
from pathlib import Path

import pytest

import ironbench.runner as runner_module
from io_core.journal import JsonlJournal
from ironbench.plant import (
    PlantSpec,
    _DeterministicNoise,
    check_requirements,
    compute_metrics,
    run_closed_loop,
)
from ironbench.runner import run_task
from ironbench.tasks import load_task

TASKS_DIR = Path(__file__).parents[1] / "src" / "ironbench" / "tasks"


def make_spec(**overrides) -> PlantSpec:
    section = {
        "model": "heater",
        "K": 60.0,
        "T": 30.0,
        "ambient": 20.0,
        "setpoint": 74.0,
        "duration": 100,
        "dt": 0.5,
        "requirements": {"steady_error": 2.0, "settle_time": 50.0, "overshoot": 10.0},
    }
    section.update(overrides)
    return PlantSpec.from_section(section)


def test_step_matches_analytic_solution():
    # y(T) = ambient + K*u - (rise)*exp(-1) for u=1: exact discretization
    spec = make_spec()
    y = spec.step(spec.ambient, 1.0, spec.ambient, spec.T)
    assert y == pytest.approx(spec.ambient + spec.K * (1 - math.exp(-1)))


def test_deterministic_noise_same_seed_same_values():
    gen1, gen2 = _DeterministicNoise(7), _DeterministicNoise(7)
    seq1 = [gen1.gauss(1.0) for _ in range(10)]
    seq2 = [gen2.gauss(1.0) for _ in range(10)]
    assert seq1 == seq2
    assert len(set(seq1)) > 1  # and it is not a constant


def test_closed_loop_clamps_actuator_and_deterministic():
    spec = make_spec(noise_std=0.5, seed=3)
    ctrl = lambda t, y, sp: 100.0
    rows1, err1 = run_closed_loop(ctrl, spec)
    rows2, err2 = run_closed_loop(ctrl, spec)
    assert err1 is None and err2 is None
    assert rows1 == rows2  # run determinism at a fixed seed
    assert all(u == spec.u_max for _, _, _, u in rows1)


def test_closed_loop_reports_non_number_return():
    rows, err = run_closed_loop(lambda t, y, sp: "heat", make_spec())
    assert rows == []
    assert "non-number" in err


def test_metrics_monotonic_heating_has_no_overshoot_and_settles():
    spec = make_spec()
    rows, _ = run_closed_loop(lambda t, y, sp: 1.0 if y < sp else 0.0, spec)
    m = compute_metrics(rows, spec)
    # hysteresis bang-bang: it overshoots the setpoint by a step but stays within tolerance
    assert m["overshoot_pct"] < 5.0
    assert 0 < m["settle_time"] < spec.duration
    assert m["steady_error"] < 2.0


def test_metrics_never_settling_is_inf():
    spec = make_spec(requirements={"steady_error": 0.001, "settle_time": 50.0})
    rows, _ = run_closed_loop(lambda t, y, sp: 0.0, spec)  # the heater is off
    m = compute_metrics(rows, spec)
    assert m["settle_time"] == math.inf
    missed = check_requirements(m, spec.requirements)
    assert missed and "never settled" in missed[-1]


def test_check_requirements_reports_facts():
    metrics = {"overshoot_pct": 12.34, "steady_error": 3.0, "settle_time": math.inf, "final_y": 1.0}
    missed = check_requirements(
        metrics, {"overshoot": 5.0, "steady_error": 2.0, "settle_time": 50.0}
    )
    assert len(missed) == 3
    assert "12.3%" in missed[0] and "5%" in missed[0]
    assert "3.00" in missed[1] and "2" in missed[1]
    assert "never settled" in missed[2]


# --- integration: run_task -> the real worker ---


def make_plant_task(tmp_path, controller: str, name="fake-plant", **task_overrides):
    d = tmp_path / "t"
    d.mkdir(exist_ok=True)
    text = f"""
name: {name}
description: fake
entry: solution.py
target: plant
timeout_sec: 5
plant:
  model: heater
  K: 60.0
  T: 10.0
  ambient: 20.0
  setpoint: 50.0
  duration: 60
  dt: 0.5
  requirements:
    steady_error: 2.0
    settle_time: 40.0
    overshoot: 5.0
expect:
"""
    text = textwrap.dedent(text)
    expect = task_overrides.get("expect", ())
    text += (
        "".join(f"  - {p!r}\n" for p in expect) if expect else "  []\n"  # an empty expect is valid
    )
    (d / "task.yaml").write_text(text, encoding="utf-8")
    (d / "solution.py").write_text(controller, encoding="utf-8")
    return load_task(d)


def test_plant_pass_and_log_tail(tmp_path):
    # GAIN 0.5: e_ss = 30/31 ~= 0.97 <= 2, saturation up to y~=48, settling ~7 s
    task = make_plant_task(
        tmp_path,
        "GAIN = 0.5\ndef control(t, y, setpoint):\n    return GAIN * (setpoint - y)\n",
    )
    res = run_task(task, out_dir=tmp_path / "out")
    assert res.passed, (res.error, res.missed)
    assert res.exit_code == 0
    log = res.serial_log.read_text("utf-8")
    assert "# metrics:" in log and "# --- summary ---" in log
    assert "K=" not in log.split("# --- summary ---")[0]  # the plant parameters never show up


def test_plant_missed_requirement_fails(tmp_path):
    task = make_plant_task(tmp_path, "def control(t, y, setpoint):\n    return 0.0\n")
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert any("steady_error" in m for m in res.missed)


def test_plant_controller_crash_is_result_not_infra(tmp_path):
    task = make_plant_task(tmp_path, "def control(t, y, setpoint):\n    return 1 / 0\n")
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "controller crashed" in res.error
    assert "controller crashed" in res.serial_log.read_text("utf-8")  # feedback to the agent
    assert not runner_module.is_infra_error(res.error)


def test_plant_controller_sys_exit_is_result(tmp_path):
    # sys.exit in the controller is also a run result: result.json/log are written,
    # the worker does not crash; the agent's exception text stays out of error (infra scan)
    controller = "import sys\ndef control(t, y, setpoint):\n    sys.exit('sensor not found')\n"
    task = make_plant_task(tmp_path, controller)
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "controller crashed" in (res.error or "")
    assert "sensor not found" not in (res.error or "")  # only in the feedback log
    assert "sensor not found" in res.serial_log.read_text("utf-8")
    assert not runner_module.is_infra_error(res.error)


def test_plant_missing_control_function(tmp_path):
    task = make_plant_task(tmp_path, "x = 1\n")
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "no control" in res.error
    assert not runner_module.is_infra_error(res.error)


def test_plant_hung_controller_hits_wall_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "WALL_GRACE_SEC", 1)
    task = make_plant_task(tmp_path, "def control(t, y, setpoint):\n    while True:\n        pass\n")
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "wall limit" in res.error
    assert not runner_module.is_infra_error(res.error)


def test_plant_missing_entry_is_infra(tmp_path):
    task = make_plant_task(tmp_path, "def control(t, y, setpoint):\n    return 0.0\n")
    (task.directory / "solution.py").unlink()
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert runner_module.is_infra_error(res.error)


def test_plant_fail_pattern_still_applies(tmp_path):
    # expect/fail patterns work on top of metrics: 'ZeroDivision' in the log is caught
    task = make_plant_task(tmp_path, "def control(t, y, setpoint):\n    return 1 / 0\n")
    import dataclasses

    task = dataclasses.replace(task, fail=("ZeroDivision",))
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert any("ZeroDivision" in p for p in res.hit_fail)


def test_plant_journal_records_start_and_result(tmp_path):
    task = make_plant_task(tmp_path, "def control(t, y, setpoint):\n    return 0.1 * (setpoint - y)\n")
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        run_task(task, out_dir=tmp_path / "out", journal=jr)
    lines = [ln for ln in jpath.read_text("utf-8").splitlines() if ln.strip()]
    import json

    events = [json.loads(ln) for ln in lines]
    kinds = [e["kind"] for e in events]
    assert "task_start" in kinds and "task_result" in kinds


# --- golden plant tasks: local, free and deterministic ---


@pytest.mark.parametrize("name", ["p-regulator", "pid-antiwindup", "system-id"])
def test_golden_plant_tasks_pass(name, tmp_path):
    task = load_task(TASKS_DIR / name)
    assert task.target == "plant"
    res = run_task(task, out_dir=tmp_path / name)
    assert res.passed, (res.error, res.missed)
