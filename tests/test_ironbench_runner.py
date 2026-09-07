"""Тесты раннера ironbench на фейковом CLI (без сети и Wokwi)."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import textwrap

import pytest
import yaml

import ironbench.runner as runner_module
from io_core.journal import JsonlJournal
from ironbench.runner import (
    TaskResult,
    _check_patterns,
    generate_paste_scenario,
    load_env_file,
    run_task,
)
from ironbench.tasks import load_task, load_tasks

# Фейковый CLI: пишет FAKE_SERIAL в --serial-log-file и выходит с кодом FAKE_EXIT
# (опционально спит FAKE_SLEEP секунд) — эмулирует контракт wokwi-cli.
FAKE_CLI = textwrap.dedent(
    """
    import os, sys, time
    args = sys.argv[1:]
    log = args[args.index("--serial-log-file") + 1]
    Path = __import__("pathlib").Path
    Path(log).write_text(os.environ.get("FAKE_SERIAL", "blink 0: on\\nblink 0: off\\n"), encoding="utf-8")
    if os.environ.get("FAKE_SLEEP"):
        time.sleep(float(os.environ["FAKE_SLEEP"]))
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
    """
)


def make_task(tmp_path, expect=("blink 0: on",), fail=(), write_entry=True, stimulus=()):
    d = tmp_path / "t"
    d.mkdir()
    text = "name: fake\n"
    if expect:
        text += "expect:\n" + "".join(f"  - {p!r}\n" for p in expect)
    if fail:
        text += "fail:\n" + "".join(f"  - {p!r}\n" for p in fail)
    if stimulus:
        text += "stimulus:\n" + "\n".join(f"  - {s}" for s in stimulus) + "\n"
    (d / "task.yaml").write_text(text, encoding="utf-8")
    # entry-файл нужен генератору paste-сценария
    if write_entry:
        (d / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return load_task(d)


def run_fake(tmp_path, task, env_extra, **kw):
    cli = [sys.executable, "-c", FAKE_CLI]
    saved = {k: os.environ.pop(k, None) for k in env_extra}
    os.environ.update({k: v for k, v in env_extra.items()})
    try:
        return run_task(task, out_dir=tmp_path / "out", cli_path=cli, **kw)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_pass_when_all_expectations_found(tmp_path):
    task = make_task(tmp_path, expect=("blink 0: on", "blink 0: off"))
    res = run_fake(tmp_path, task, {})
    assert isinstance(res, TaskResult)
    assert res.passed
    assert res.exit_code == 0
    assert res.missed == ()
    assert res.serial_log is not None and res.serial_log.is_file()


def test_fail_on_missed_pattern(tmp_path):
    task = make_task(tmp_path, expect=("blink 99: on",))
    res = run_fake(tmp_path, task, {})
    assert not res.passed
    assert res.missed == ("blink 99: on",)


def test_fail_on_fail_pattern(tmp_path):
    task = make_task(tmp_path, expect=("blink 0: on",), fail=("panic",))
    res = run_fake(tmp_path, task, {"FAKE_SERIAL": "blink 0: on\npanic: oops\n"})
    assert not res.passed
    assert res.hit_fail == ("panic",)


def test_fail_on_cli_error_exit(tmp_path):
    task = make_task(tmp_path)
    res = run_fake(tmp_path, task, {"FAKE_EXIT": "1"})
    assert not res.passed
    assert res.exit_code == 1


def test_timeout_exit_with_full_patterns_passes(tmp_path):
    # Бесконечный цикл прошивки: wokwi-cli выходит по --timeout (42),
    # но все expect-паттерны в serial найдены → задача пройдена
    task = make_task(tmp_path)
    res = run_fake(tmp_path, task, {"FAKE_EXIT": "42"})
    assert res.passed
    assert res.exit_code == 42


def test_timeout_exit_with_missed_pattern_fails(tmp_path):
    task = make_task(tmp_path, expect=("blink 99: on",))
    res = run_fake(tmp_path, task, {"FAKE_EXIT": "42"})
    assert not res.passed
    assert res.exit_code == 42
    assert res.missed == ("blink 99: on",)


def test_wall_clock_timeout_kills_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "WALL_GRACE_SEC", 1)
    task = dataclasses.replace(make_task(tmp_path), timeout_sec=0)
    res = run_fake(tmp_path, task, {"FAKE_SLEEP": "10"})
    assert not res.passed
    assert res.exit_code is None
    assert "таймаут" in (res.error or "")


def test_missing_cli_reports_error(tmp_path):
    task = make_task(tmp_path)
    res = run_task(task, out_dir=tmp_path / "out", cli_path="no-such-cli-xyz")
    assert not res.passed
    assert "не найден" in (res.error or "")


def test_missing_entry_file_is_clean_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "WALL_GRACE_SEC", 1)
    task = make_task(tmp_path, write_entry=False)
    res = run_fake(tmp_path, task, {})
    assert not res.passed
    assert "подготовить задачу" in (res.error or "")


def test_check_patterns_regex():
    missed, hit = _check_patterns("blink 3: on\n", ("blink \\d+: on", "blink 9: off"), ())
    assert missed == ("blink 9: off",)
    assert hit == ()


def test_stage_task_pulls_shared_firmware(tmp_path):
    from ironbench.runner import FIRMWARE_DIR, _stage_task

    task = make_task(tmp_path)
    bin_name = "ESP32_GENERIC-20251209-v1.27.0.bin"
    assert (FIRMWARE_DIR / bin_name).is_file(), "общая прошивка должна быть в tasks/_firmware"
    (task.directory / "wokwi.toml").write_text(
        f'[wokwi]\nversion = 1\nelf = "{bin_name}"\nfirmware = "{bin_name}"\n',
        encoding="utf-8",
    )
    stage, _scenario = _stage_task(task, tmp_path / "out")
    assert (stage / bin_name).is_file(), "стейдж должен подтянуть бин из FIRMWARE_DIR"


def test_journal_records_start_and_result(tmp_path):
    task = make_task(tmp_path)
    jpath = tmp_path / "journal.jsonl"
    with JsonlJournal(jpath, actor="ironbench") as jr:
        run_fake(tmp_path, task, {}, journal=jr)
    events = [json.loads(line) for line in jpath.read_text(encoding="utf-8").splitlines()]
    kinds = [e["kind"] for e in events]
    assert "task_start" in kinds and "task_result" in kinds
    result_event = events[-1]
    assert result_event["actor"] == "ironbench"
    assert result_event["task"] == "fake"
    assert isinstance(result_event["passed"], bool)


def test_load_env_file_formats(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# комментарий\n"
        "PLAIN=value1\n"
        'QUOTED="value 2"\n'
        "export EXPORTED=value3\n"
        '$env:PS_TOKEN="wok_xxx"\n'
        "$env:PS_SINGLE='wok yyy'\n"
        "=invalid\n",
        encoding="utf-8",
    )
    env = load_env_file(env_file)
    assert env == {
        "PLAIN": "value1",
        "QUOTED": "value 2",
        "EXPORTED": "value3",
        "PS_TOKEN": "wok_xxx",
        "PS_SINGLE": "wok yyy",
    }


def test_load_env_file_missing(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == {}


def test_task_yaml_loads_from_packaged_blink():
    tasks_dir = runner_module.Path(__file__).parents[1] / "src" / "ironbench" / "tasks"
    task = load_task(tasks_dir / "blink")
    assert task.name == "blink"
    assert task.timeout_sec == 20
    assert any("blink 0: on" == p for p in task.expect)
    assert task.scenario is None  # используется генерация REPL-paste сценария
    assert task.entry == "solution.py"


def test_all_golden_tasks_have_required_files():
    tasks_dir = runner_module.Path(__file__).parents[1] / "src" / "ironbench" / "tasks"
    tasks = load_tasks(tasks_dir)
    assert len(tasks) >= 6  # blink + 5 золотых задач этапа 2.3
    for task in tasks:
        assert (task.directory / "wokwi.toml").is_file(), task.name
        assert (task.directory / "diagram.json").is_file(), task.name
        assert (task.directory / "task.yaml").is_file(), task.name
        assert (task.directory / task.entry).is_file(), task.name


def test_generate_paste_scenario(tmp_path):
    task = make_task(tmp_path, expect=("blink 0: on", r"blink \d+: off"))
    (task.directory / "main.py").write_text("print('hi')\n", encoding="utf-8")
    doc = yaml.safe_load(generate_paste_scenario(task))
    steps = doc["steps"]
    assert steps[0] == {"wait-serial": ">>>"}
    writes = [s.get("write-serial", "") for s in steps]
    assert any("\x05" in w for w in writes)  # Ctrl+E — вход в raw-paste
    assert any("print('hi')" in w for w in writes)
    assert any("\x04" in w for w in writes)  # Ctrl+D — выполнить
    # литеральный expect превращается в wait-serial, regex-паттерн — нет
    wait_serials = [s["wait-serial"] for s in steps if "wait-serial" in s]
    assert "blink 0: on" in wait_serials
    assert r"blink \d+: off" not in wait_serials


def test_generate_paste_scenario_with_stimulus(tmp_path):
    task = make_task(tmp_path, expect=("echo: hi",), stimulus=['write-serial: "hi\\r"'])
    steps = yaml.safe_load(generate_paste_scenario(task))["steps"]
    # stimulus идёт после Ctrl+D (\x04) и до последнего wait-serial (литеральный expect)
    stim_idx = steps.index({"write-serial": "hi\r"})
    ctrl_d_idx = steps.index({"write-serial": "\x04"})
    last_wait_idx = max(i for i, s in enumerate(steps) if "wait-serial" in s)
    assert ctrl_d_idx < stim_idx < last_wait_idx


def test_paste_scenario_reads_entry_not_main(tmp_path):
    # entry-файл (solution.py у золотых задач), а не main.py — маршрутизация по task.entry
    tasks_dir = runner_module.Path(__file__).parents[1] / "src" / "ironbench" / "tasks"
    task = load_task(tasks_dir / "blink")
    steps = yaml.safe_load(generate_paste_scenario(task))["steps"]
    code = next(s["write-serial"] for s in steps if "while True" in s.get("write-serial", ""))
    assert "machine import Pin" in code  # это содержимое solution.py


def test_malformed_wokwi_toml_is_clean_fail(tmp_path):
    task = make_task(tmp_path)
    (task.directory / "wokwi.toml").write_text("[wokwi\nbroken ===", encoding="utf-8")
    res = run_fake(tmp_path, task, {})
    assert not res.passed
    assert "подготовить задачу" in (res.error or "")


def test_missing_firmware_is_clean_fail(tmp_path):
    task = make_task(tmp_path)
    (task.directory / "wokwi.toml").write_text(
        '[wokwi]\nversion = 1\nelf = "no-such.bin"\nfirmware = "no-such.bin"\n',
        encoding="utf-8",
    )
    res = run_fake(tmp_path, task, {})
    assert not res.passed
    assert "no-such.bin" in (res.error or "")


# --- мишени (план 3.2): диспетчер run_task ---


def test_unknown_target_rejected(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "task.yaml").write_text("name: fake\ntarget: qemu\n", encoding="utf-8")
    with pytest.raises(ValueError, match="неизвестная мишень"):
        load_task(d)


def test_explicit_wokwi_target_loads(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "task.yaml").write_text("name: fake\ntarget: wokwi\n", encoding="utf-8")
    assert load_task(d).target == "wokwi"


@pytest.mark.parametrize("target,stage_note", [("renode", "2.6"), ("real", "этап 3")])
def test_unready_target_is_clean_fail_without_cli(tmp_path, monkeypatch, target, stage_note):
    task = dataclasses.replace(make_task(tmp_path), target=target)

    # wokwi-бэкенд не должен зваться для нереализованных мишеней
    def forbidden_wokwi(*a, **k):
        raise AssertionError("wokwi-бэкенд вызван для нереализованной мишени")

    monkeypatch.setattr(runner_module, "_run_wokwi", forbidden_wokwi)
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert res.exit_code is None
    assert stage_note in (res.error or "")
    assert "не реализована" in (res.error or "")
    assert res.missed == task.expect  # проверки не выполнялись


def test_unready_target_journaled(tmp_path):
    task = dataclasses.replace(make_task(tmp_path), target="real")
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        run_task(task, out_dir=tmp_path / "out", cli_path=["never"], journal=jr)
    kinds = [json.loads(line)["kind"] for line in jpath.read_text(encoding="utf-8").splitlines()]
    assert kinds == ["task_result"]  # task_start нет — запуска не было
