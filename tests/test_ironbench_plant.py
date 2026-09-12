"""Tests of the plant target (a closed "plant + controller" loop, see
ironbench/plant.py and runner_plant.py): unit tests of the physics/metrics +
integration through the real two-process runner (a local subprocess, no WSL
or simulators). IH-25: the physics lives in the harness process, the
controller answers over pipes - K/T never enter the controller process."""

from __future__ import annotations

import json
import math
import textwrap
from pathlib import Path

import pytest

import ironbench.runner as runner_module
from io_core.journal import JsonlJournal
from ironbench import runner_common
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


# --- integration: run_task -> the two-process runner (IH-25) ---


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


PASSING_CONTROLLER = "GAIN = 0.5\ndef control(t, y, setpoint):\n    return GAIN * (setpoint - y)\n"


def test_plant_pass_and_log_tail(tmp_path):
    # GAIN 0.5: e_ss = 30/31 ~= 0.97 <= 2, saturation up to y~=48, settling ~7 s
    task = make_plant_task(tmp_path, PASSING_CONTROLLER)
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
    assert "ZeroDivisionError" in res.serial_log.read_text("utf-8")  # feedback to the agent
    assert not runner_module.is_infra_error(res)
    assert res.error_kind == "run"


def test_plant_controller_sys_exit_is_result(tmp_path):
    # sys.exit in the controller is also a run result: the reason stays in the
    # feedback log, the agent's exception text does not leak into error (infra scan)
    controller = "import sys\ndef control(t, y, setpoint):\n    sys.exit('sensor not found')\n"
    task = make_plant_task(tmp_path, controller)
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "controller" in (res.error or "")
    assert "sensor not found" not in (res.error or "")  # only in the feedback log
    assert "sensor not found" in res.serial_log.read_text("utf-8")
    assert not runner_module.is_infra_error(res)
    assert res.error_kind == "run"


def test_plant_missing_control_function(tmp_path):
    task = make_plant_task(tmp_path, "x = 1\n")
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "no control" in res.error
    assert not runner_module.is_infra_error(res)
    assert res.error_kind == "run"


def test_plant_hung_controller_hits_wall_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_common, "WALL_GRACE_SEC", 1)
    task = make_plant_task(tmp_path, "def control(t, y, setpoint):\n    while True:\n        pass\n")
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "wall limit" in res.error
    assert not runner_module.is_infra_error(res)
    assert res.error_kind == "timeout"


def test_plant_missing_entry_is_infra(tmp_path):
    task = make_plant_task(tmp_path, "def control(t, y, setpoint):\n    return 0.0\n")
    (task.directory / "solution.py").unlink()
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert runner_module.is_infra_error(res)
    assert res.error_kind == "infra"


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
    events = [json.loads(ln) for ln in lines]
    kinds = [e["kind"] for e in events]
    assert "task_start" in kinds and "task_result" in kinds


def test_plant_run_dirs_are_unique_per_run(tmp_path):
    task = make_plant_task(tmp_path, PASSING_CONTROLLER)
    out = tmp_path / "out"
    res1 = run_task(task, out_dir=out)
    res2 = run_task(task, out_dir=out)
    assert res1.passed and res2.passed
    assert res1.serial_log != res2.serial_log
    assert res1.serial_log.is_file() and res2.serial_log.is_file()
    assert res1.serial_log.parent.parent == out / "fake-plant"


def test_plant_controller_prints_land_in_feedback_log(tmp_path):
    controller = (
        "def control(t, y, setpoint):\n"
        "    print(f'step {t}')\n"
        "    return 0.5 * (setpoint - y)\n"
    )
    task = make_plant_task(tmp_path, controller)
    res = run_task(task, out_dir=tmp_path / "out")
    assert res.passed, (res.error, res.missed)
    log = res.serial_log.read_text("utf-8")
    assert "# controller stdout (tail):" in log
    # the tail keeps the LAST 30 stderr lines - 120 steps means step 0 has
    # scrolled away; a late step line proves prints land in the feedback
    assert "step 4" in log


# --- IH-25: spec-reader isolation ---


SPEC_HUNTER = textwrap.dedent(
    """
    import json, os, sys

    # the task's real parameters (the hunter knows them, like a real cheater
    # who read the repo): if any of them are reachable from the process view,
    # the isolation is broken
    secrets = ["60.0", "10.0", "60", "10"]

    def looks_like_secret(text):
        return any(s in text for s in secrets)

    hits = []
    # argv: the old design leaked the spec file location here
    for arg in sys.argv:
        if looks_like_secret(arg):
            hits.append(["argv", arg])
    # environment: an EXACT value match (substring hits in unrelated env
    # vars are noise, not parameter leaks)
    for k, v in os.environ.items():
        if v.strip() in secrets:
            hits.append(["env", k])
    # the cwd inventory: file NAMES must not carry the parameters
    for name in os.listdir("."):
        if looks_like_secret(name):
            hits.append(["cwd-name", name])
    # the controller's own entry: it legitimately contains the literals (the
    # hunter declares them itself), so strip its own declarations before the
    # content check - what must NOT be there is spec content from OUTSIDE
    with open("controller_entry.py", encoding="utf-8") as fh:
        entry_text = "".join(
            ln for ln in fh if "secrets =" not in ln and "looks_like_secret" not in ln
        )
    if looks_like_secret(entry_text):
        hits.append(["entry", "controller_entry.py"])
    # the conventional places a cheater would try first
    for name in ("task.yaml", "spec.json", "plant.json", "plant-spec.json"):
        if os.path.exists(name):
            hits.append(["exists", name])
    # printed to the feedback log: the harness never gives the controller a
    # writeable channel into scoring, so the report travels the same way
    print("SPEC-HUNT:", json.dumps(hits))

    def control(t, y, setpoint):
        return 0.5 * (setpoint - y)
    """
)


def test_plant_spec_reader_finds_nothing(tmp_path):
    # THE IH-25 pin: a controller that actively hunts for the plant
    # parameters must not find K/T anywhere in its process view - not in
    # argv, not in the environment, not in cwd file names, not in the
    # conventional spec locations (the old design leaked the spec location
    # in argv; the new one copies the entry into a throwaway cwd and keeps
    # the physics in the harness process). The hunter reports through the
    # feedback log - the only channel it has.
    import json as json_mod

    task = make_plant_task(tmp_path, SPEC_HUNTER)
    res = run_task(task, out_dir=tmp_path / "out")
    assert res.passed, (res.error, res.missed)
    log = res.serial_log.read_text("utf-8")
    hunt_lines = [ln for ln in log.splitlines() if "SPEC-HUNT:" in ln]
    assert len(hunt_lines) == 1, hunt_lines
    hits = json_mod.loads(hunt_lines[0].split("SPEC-HUNT:", 1)[1])
    assert hits == [], f"the controller found plant parameters: {hits}"


def test_plant_controller_cannot_influence_scoring_via_files(tmp_path):
    # the old result-file path is gone: scoring is computed in the harness
    # from the pipe data - a controller writing a fake result file into its
    # cwd can never turn it into a PASS
    cheater = (
        "import json, pathlib\n"
        "pathlib.Path('fake-result.json').write_text("
        "json.dumps({'error': None, 'metrics': {'overshoot_pct': 0.0, 'steady_error': 0.0,"
        " 'settle_time': 0.0, 'final_y': 50.0}, 'missed': [], 'steps': 120, 'run_id': 'x'}))\n"
        "def control(t, y, setpoint):\n"
        "    return 0.0\n"  # fails every requirement
    )
    task = make_plant_task(tmp_path, cheater)
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert any("steady_error" in m for m in res.missed)


def test_plant_hostile_non_utf8_output_is_clean_fail(tmp_path):
    # IH-25 review: the controller prints raw non-UTF8 bytes - the pipes
    # decode with errors="replace", so the run degrades into a clean FAIL
    # with the feedback preserved, and the pump threads do not die.
    controller = (
        "import sys\n"
        "def control(t, y, setpoint):\n"
        "    sys.stderr.buffer.write(bytes([0xff, 0xfe, 0x0a]))\n"
        "    sys.stderr.buffer.flush()\n"
        "    return 0.0\n"
    )
    task = make_plant_task(tmp_path, controller)
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert any("steady_error" in m for m in res.missed)
    log = res.serial_log.read_text("utf-8", errors="replace")
    assert "�" in log  # the bytes arrived as replacement chars, not a crash


def test_plant_rerun_replaces_not_doubles(tmp_path):
    # a re-run into the same out_dir scores its own run only (per-run dirs);
    # a controller replaced with a dying one must fail the second run as a
    # run result
    task = make_plant_task(tmp_path, PASSING_CONTROLLER)
    out = tmp_path / "out"
    res1 = run_task(task, out_dir=out)
    assert res1.passed, (res1.error, res1.missed)
    (task.directory / "solution.py").write_text(
        "def control(t, y, setpoint):\n    import os\n    os._exit(0)\n",
        encoding="utf-8",
    )
    res2 = run_task(task, out_dir=out)
    assert not res2.passed
    assert res2.error_kind == "run"  # died mid-run: a run result, not infra


# --- golden plant tasks: local, free and deterministic ---


@pytest.mark.parametrize("name", ["p-regulator", "pid-antiwindup", "system-id"])
def test_golden_plant_tasks_pass(name, tmp_path):
    task = load_task(TASKS_DIR / name)
    assert task.target == "plant"
    res = run_task(task, out_dir=tmp_path / name)
    assert res.passed, (res.error, res.missed)
