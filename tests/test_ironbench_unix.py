"""Тесты мишени unix (MicroPython unix-port в WSL2): фейковый micropython —
локальный скрипт, читающий stdin и печатающий в stdout. Без WSL и сборки.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import ironbench.runner as runner_module
from io_core.journal import JsonlJournal
from ironbench.runner import run_task
from ironbench.tasks import load_task

# Фейковый micropython: печатает баннер, потом эхо строк из stdin (как uart-echo).
# Режимы: echo / missed (баннер и выход) / crash (ненулевой exit) / hang (вечный
# цикл без stdin) / finite (баннер и чистый выход 0).
FAKE_UPY = textwrap.dedent(
    """
    import sys, time
    mode = sys.argv[1]
    print("boot ok", flush=True)
    if mode == "crash":
        sys.exit(3)
    if mode == "finite":
        sys.exit(0)
    if mode == "hang":
        while True:
            time.sleep(0.2)
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        print("echo: " + line.strip(), flush=True)
    """
)


def make_unix_task(tmp_path, expect=("boot ok",), stimulus=(), timeout_sec=5):
    d = tmp_path / "t"
    d.mkdir()
    text = f"""
name: fake-unix
description: фейк
entry: solution.py
target: unix
timeout_sec: {timeout_sec}
expect:
"""
    text += "".join(f"  - {p!r}\n" for p in expect)
    if stimulus:
        text += "stimulus:\n" + "\n".join(f"  - {s}" for s in stimulus) + "\n"
    (d / "task.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    (d / "solution.py").write_text('print("hi")\n', encoding="utf-8")
    return load_task(d)


def run_fake_unix(tmp_path, task, mode, **kw):
    script = tmp_path / "fake_upy.py"
    script.write_text(FAKE_UPY, encoding="utf-8")
    cmd = [sys.executable, str(script), mode]
    return run_task(task, out_dir=tmp_path / "out", unix_cmd=cmd, **kw)


@pytest.fixture(autouse=True)
def _fast_deadlines(monkeypatch):
    # missed-тесты по смыслу ждут до дедлайна — не ждём 25 реальных секунд
    monkeypatch.setattr(runner_module, "WALL_GRACE_SEC", 1)


def test_unix_pass_with_stimulus_cr_translation(tmp_path):
    # \r из wokwi-стимула переводится в \n: эхо отвечает на строку.
    # Прошивка бесконечна — после матчинга её гасят, exit_code None
    # (stdin не закрывают: EOF у input() дал бы Traceback в логе)
    task = make_unix_task(
        tmp_path,
        expect=("boot ok", "echo: hello"),
        stimulus=['delay: 50ms', 'write-serial: "hello\\r"'],
    )
    res = run_fake_unix(tmp_path, task, "echo")
    assert res.passed, res.error
    assert res.exit_code is None
    assert res.missed == ()
    assert res.serial_log is not None and "echo: hello" in res.serial_log.read_text("utf-8")
    assert "Traceback" not in res.serial_log.read_text("utf-8")


def test_unix_finite_program_exits_zero(tmp_path):
    task = make_unix_task(tmp_path)
    res = run_fake_unix(tmp_path, task, "finite")
    assert res.passed
    assert res.exit_code == 0


def test_unix_fail_on_missed_pattern(tmp_path):
    task = make_unix_task(tmp_path, expect=("never printed",), timeout_sec=0)
    res = run_fake_unix(tmp_path, task, "echo")
    assert not res.passed
    assert res.missed == ("never printed",)


def test_unix_fail_on_missed_pattern_with_expected_present(tmp_path):
    task = make_unix_task(tmp_path, expect=("boot ok", "nope"), timeout_sec=0)
    res = run_fake_unix(tmp_path, task, "echo")
    assert not res.passed
    assert res.missed == ("nope",)


def test_unix_nonzero_exit_is_error(tmp_path):
    task = make_unix_task(tmp_path)
    res = run_fake_unix(tmp_path, task, "crash")
    assert not res.passed
    assert res.exit_code == 3
    assert "завершился с кодом 3" in (res.error or "")


def test_unix_hang_until_wall_deadline(tmp_path):
    # паттерн не печатается никогда — задача фейлится по дедлайну, процесс гасится
    task = make_unix_task(tmp_path, expect=("never printed",), timeout_sec=0)
    res = run_fake_unix(tmp_path, task, "hang")
    assert not res.passed
    assert res.exit_code is None  # убит по дедлайну, не завершился сам
    assert res.missed == task.expect


def test_unix_set_control_rejected_upfront(tmp_path):
    task = make_unix_task(tmp_path, stimulus=['set-control: "button0: true"'])
    res = run_fake_unix(tmp_path, task, "echo")
    assert not res.passed
    assert "set-control" in (res.error or "")
    # отказ неисправим для агента — solve-цикл должен выходить сразу, не жечь итерации
    assert runner_module.is_infra_error(res.error)


def test_unix_journal_records_start_and_result(tmp_path):
    task = make_unix_task(tmp_path)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        run_fake_unix(tmp_path, task, "echo", journal=jr)
    events = [json.loads(line) for line in jpath.read_text("utf-8").splitlines()]
    kinds = [e["kind"] for e in events]
    assert "task_start" in kinds and "task_result" in kinds
    assert events[-1]["passed"] is True


def test_unix_without_cmd_pushes_entry_and_runs_wsl(tmp_path, monkeypatch):
    task = make_unix_task(tmp_path)
    runs, popens = [], []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"STAGE-PUSHED\n", stderr=b"")

    def fake_popen(cmd, **kw):
        popens.append(cmd)
        raise FileNotFoundError("micropython")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "не найден" in (res.error or "")
    assert len(runs) == 1 and "ironharness-runs" in runs[0][-1]
    assert popens[0][:3] == ["wsl", "-d", "OpenClawGateway"]
    assert "~/bin/micropython" in popens[0][-1]
    assert popens[0][-1].startswith("exec ")  # micropython замещает bash


def test_unix_cli_target_override(tmp_path):
    # --target unix переопределяет мишень wokwi-задачи: диспетчер уходит в unix
    from ironbench.tasks import load_tasks

    tasks = load_tasks(Path(__file__).parents[1] / "src" / "ironbench" / "tasks")
    uart = next(t for t in tasks if t.name == "uart-echo")
    assert uart.target == "wokwi"
    import dataclasses

    assert dataclasses.replace(uart, target="unix").target == "unix"


def make_noise_task(tmp_path, noise_yaml, stimulus, expect=("echo: one",), timeout_sec=0):
    d = tmp_path / "tn"
    d.mkdir()
    text = f"""
name: fake-noise
description: фейк
entry: solution.py
target: unix
timeout_sec: {timeout_sec}
{noise_yaml}
expect:
"""
    text += "".join(f"  - {p!r}\n" for p in expect)
    text += "stimulus:\n" + "\n".join(f"  - {s}" for s in stimulus) + "\n"
    (d / "task.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    (d / "solution.py").write_text('print("hi")\n', encoding="utf-8")
    return load_task(d)


def test_unix_noise_drop_swallows_write_step(tmp_path):
    # op2 выбрасывается линией: тело two не доходит, aaa/ccc доходят
    noise = """
noise:
  seed: 7
  faults:
    - action: drop
      after_ops: 1
      count: 1
"""
    stimulus = [
        'write-serial: "one\\r"',
        'write-serial: "two\\r"',
        'write-serial: "three\\r"',
    ]
    task = make_noise_task(
        tmp_path, noise, stimulus, expect=("echo: one", "echo: three")
    )
    res = run_fake_unix(tmp_path, task, "echo")
    assert res.passed, res.error
    log = res.serial_log.read_text("utf-8")
    assert "echo: two" not in log
    assert "echo: one" in log and "echo: three" in log


def test_unix_noise_corrupt_changes_bytes_then_retry_is_clean(tmp_path):
    # op1 портится всеми байтами (ratio 1.0), ретрай op2 доходит чисто
    noise = """
noise:
  seed: 7
  faults:
    - action: corrupt
      after_ops: 0
      count: 1
      ratio: 1.0
"""
    stimulus = [
        'write-serial: "one\\r"',
        'write-serial: "one\\r"',
    ]
    task = make_noise_task(tmp_path, noise, stimulus, expect=("echo: one",))
    res = run_fake_unix(tmp_path, task, "echo")
    assert res.passed, res.error
    log = res.serial_log.read_text("utf-8")
    assert "echo: one" in log
    assert log.count("echo: ") == 2  # битая строка тоже напечатана, но другой


def test_unix_noise_validation_bad_action(tmp_path):
    noise = """
noise:
  seed: 7
  faults:
    - action: explode
      count: 1
"""
    with pytest.raises(ValueError, match="noise.faults"):
        make_noise_task(tmp_path, noise, ['write-serial: "x\\r"'])


def test_unix_noise_validation_bad_seed(tmp_path):
    noise = """
noise:
  seed: abc
  faults: []
"""
    with pytest.raises(ValueError, match="noise.seed"):
        make_noise_task(tmp_path, noise, ['write-serial: "x\\r"'])


def test_unix_noise_validation_disconnect_rejected(tmp_path):
    # disconnect из шумной линии не поддержан: валидатор режет на загрузке
    noise = """
noise:
  seed: 7
  faults:
    - action: disconnect
      after_ops: 1
"""
    with pytest.raises(ValueError, match="disconnect"):
        make_noise_task(tmp_path, noise, ['write-serial: "x\\r"'])


def test_unix_noise_rejected_on_wokwi_target(tmp_path):
    d = tmp_path / "tw"
    d.mkdir()
    text = """
name: fake-wokwi-noise
description: фейк
entry: main.py
target: wokwi
timeout_sec: 5
noise:
  seed: 7
  faults:
    - action: drop
      count: 1
expect:
  - 'x'
"""
    (d / "task.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    with pytest.raises(ValueError, match="только мишенью unix"):
        load_task(d)
