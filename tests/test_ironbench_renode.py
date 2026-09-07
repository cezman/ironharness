"""Тесты мишени renode (этап 2.6): фейковый «Renode» — локальный TCP-сервер,
говорящий протоколом MicroPython REPL. Без WSL, Renode и сети.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import sys
import textwrap

import pytest

import ironbench.runner as runner_module
from io_core.journal import JsonlJournal
from ironbench.runner import (
    _parse_delay,
    _stage_renode_task,
    generate_renode_resc,
    is_infra_error,
    run_task,
)
from ironbench.tasks import load_task

# Фейковый Renode: слушает TCP-порт, ведёт диалог по REPL-протоколу раннера
# (nudge → Ctrl+E paste mode → код до Ctrl+D → ответ по режиму).
# Режимы: ok / missed / traceback / nopaste / nolisten / hang / echo.
FAKE_RENODE = textwrap.dedent(
    """
    import socket, sys
    port, mode = int(sys.argv[1]), sys.argv[2]
    if mode == "nolisten":
        sys.exit(0)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    conn, _ = srv.accept()
    conn.settimeout(5)
    buf = b""
    banner = paste = False
    while b"\\x04" not in buf:
        data = conn.recv(4096)
        if not data:
            break
        buf += data
        if mode == "hang":
            continue
        if b"\\x05" in buf and not paste:
            paste = True
            if mode == "nopaste":
                conn.sendall(b"unknown control\\r\\n>>> ")
            else:
                conn.sendall(b"\\r\\npaste mode; Ctrl-C to cancel, Ctrl-D to finish\\r\\n=== ")
        elif b"\\x05" not in buf and not banner:
            banner = True
            conn.sendall(b"fake MicroPython v0\\r\\n>>> ")
    if mode == "ok":
        conn.sendall(b"alpha bravo\\r\\ncharlie delta\\r\\nbye now\\r\\n>>> ")
    elif mode == "missed":
        conn.sendall(b"nothing useful\\r\\n>>> ")
    elif mode == "traceback":
        conn.sendall(b"Traceback (most recent call last):\\r\\nMemoryError\\r\\n>>> ")
    elif mode == "echo":
        conn.settimeout(5)
        data = conn.recv(4096)
        conn.sendall(b"echo: " + data.strip(b"\\r\\n") + b"\\r\\n>>> ")
    conn.close()
    """
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_renode_task(tmp_path, expect=("alpha bravo",), fail=(), stimulus=(), timeout_sec=5):
    d = tmp_path / "t"
    d.mkdir()
    text = f"""
name: fake-rn
description: фейк
entry: solution.py
target: renode
timeout_sec: {timeout_sec}
renode:
  platform: fake_platform
  firmware: fake.elf
expect:
"""
    text += "".join(f"  - {p!r}\n" for p in expect)
    if fail:
        text += "fail:\n" + "".join(f"  - {p!r}\n" for p in fail)
    if stimulus:
        text += "stimulus:\n" + "\n".join(f"  - {s}" for s in stimulus) + "\n"
    (d / "task.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    (d / "solution.py").write_text('print("hi")\n', encoding="utf-8")
    (d / "fake.elf").write_bytes(b"\x7fELF-fake")
    return load_task(d)


def run_fake_renode(tmp_path, task, mode, **kw):
    script = tmp_path / "fake_renode.py"
    script.write_text(FAKE_RENODE, encoding="utf-8")
    port = free_port()
    mp = pytest.MonkeyPatch()
    mp.setenv("IRONBENCH_RENODE_PORT", str(port))
    cmd = [sys.executable, str(script), str(port), mode]
    try:
        return run_task(task, out_dir=tmp_path / "out", renode_cmd=cmd, **kw)
    finally:
        mp.undo()


# --- утилиты ---


def test_parse_delay():
    assert _parse_delay("1500ms") == 1.5
    assert _parse_delay("2s") == 2.0
    assert _parse_delay("0.5") == 0.5


def test_generate_resc_contains_terminal_and_firmware_placeholder(tmp_path):
    task = make_renode_task(tmp_path)
    resc = generate_renode_resc(task, 1234)
    assert "platforms/cpus/fake_platform.repl" in resc
    assert "CreateServerSocketTerminal 1234" in resc
    assert "connector Connect sysbus.uart term" in resc
    # маркер пути @ обязан остаться перед плейсхолдером (иначе монетор не съест путь)
    assert "LoadELF @__FIRMWARE__" in resc


def test_telnet_filter_strips_iac_and_unescapes_ff():
    tel = runner_module._TelnetFilter()
    # согласование + данные с экранированным 0xFF и IAC, разорванным между чанками;
    # литеральные 0xFF — не UTF-8, при lossy-декоде дают U+FFFD
    out = tel.feed(b"\xff\xfd\x00\xff\xfb\x01he")
    out += tel.feed(b"\xff")
    out += tel.feed(b"\xffllo\xff\xffworld")
    assert out == "he\ufffdllo\ufffdworld"
    assert "\x01" not in out


def test_telnet_filter_split_iac_waits_for_next_chunk():
    # WILL-опция и IAC SE, разорванные между чанками, не должны ни глотать данные
    # из следующего чанка, ни навсегда глушить фильтр внутри SB
    tel = runner_module._TelnetFilter()
    out = tel.feed(b"\xff\xfb")  # WILL без опции
    out += tel.feed(b"\x01data")  # опция 0x01 не должна попасть в данные
    assert out == "data"
    tel = runner_module._TelnetFilter()
    out = tel.feed(b"\xff\xfa\x18value\xff")  # SB без SE — SE на границе чанков
    out += tel.feed(b"\xf0rest")
    assert out == "rest"


def test_read_port_line_skips_noise_lines():
    import io

    stream = io.BytesIO(
        b"wsl: Detected localhost proxy configuration...\n"
        b"RENODE_PORT=54321\n"
    )
    assert runner_module._read_port_line(stream, timeout=2) == 54321
    # мусор вместо числа после '=' — строка пропускается (до EOF → ошибка)
    with pytest.raises(ConnectionError, match="не сообщил порт"):
        runner_module._read_port_line(io.BytesIO(b"RENODE_PORT=\n"), timeout=0.3)


def test_paste_code_drops_comment_lines():
    code = "# шапка\nprint('a')\n    # вложенный комментарий\nprint('b')  # хвост остаётся\n"
    assert runner_module._paste_code(code) == "print('a')\nprint('b')  # хвост остаётся"


def test_stage_requires_renode_section(tmp_path):
    task = dataclasses.replace(make_renode_task(tmp_path), renode={})
    with pytest.raises(ValueError, match="platform и firmware"):
        _stage_renode_task(task, tmp_path / "out", 3456)


def test_stage_missing_firmware_is_clean_fail(tmp_path):
    task = make_renode_task(tmp_path)
    (task.directory / "fake.elf").unlink()
    with pytest.raises(ValueError, match="fake.elf"):
        _stage_renode_task(task, tmp_path / "out", 3456)


# --- полный прогон через диспетчер на фейковом Renode ---


def test_renode_pass_when_all_patterns_printed(tmp_path):
    task = make_renode_task(tmp_path, expect=("alpha bravo", "charlie delta", "bye now"))
    res = run_fake_renode(tmp_path, task, "ok")
    assert res.passed, res.error
    assert res.exit_code == 0
    assert res.missed == ()
    assert res.serial_log is not None and "alpha bravo" in res.serial_log.read_text("utf-8")


def test_renode_fail_on_missed_pattern(tmp_path):
    task = make_renode_task(tmp_path, expect=("alpha bravo", "never printed"))
    res = run_fake_renode(tmp_path, task, "ok")
    assert not res.passed
    assert res.missed == ("never printed",)


def test_renode_fail_on_fail_pattern(tmp_path):
    task = make_renode_task(tmp_path, expect=("alpha bravo",), fail=("MemoryError",))
    res = run_fake_renode(tmp_path, task, "traceback")
    assert not res.passed
    assert res.hit_fail == ("MemoryError",)


def test_renode_stimulus_write_serial(tmp_path):
    task = make_renode_task(
        tmp_path,
        expect=("echo: ping42",),
        stimulus=["delay: 50ms", 'write-serial: "ping42\\r"'],
    )
    res = run_fake_renode(tmp_path, task, "echo")
    assert res.passed, res.error


def test_renode_nopaste_is_infra_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "RENODE_STEP_SEC", 2)
    task = make_renode_task(tmp_path)
    res = run_fake_renode(tmp_path, task, "nopaste")
    assert not res.passed
    assert "paste mode" in (res.error or "")
    assert is_infra_error(res.error)


def test_renode_no_listener_is_infra_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "RENODE_CONNECT_SEC", 3)
    task = make_renode_task(tmp_path)
    res = run_fake_renode(tmp_path, task, "nolisten")
    assert not res.passed
    assert "не удалось подключиться" in (res.error or "")
    assert is_infra_error(res.error)


def test_renode_hang_repl_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_module, "WALL_GRACE_SEC", 1)
    task = make_renode_task(tmp_path, timeout_sec=0)
    res = run_fake_renode(tmp_path, task, "hang")
    assert not res.passed
    assert res.missed == task.expect  # тишина в serial → всё не найдено


def test_renode_set_control_rejected_upfront(tmp_path):
    task = make_renode_task(tmp_path, stimulus=['set-control: "button0: true"'])
    res = run_fake_renode(tmp_path, task, "ok")
    assert not res.passed
    assert "set-control" in (res.error or "")
    assert res.missed == task.expect


def test_renode_journal_records_start_and_result(tmp_path):
    task = make_renode_task(tmp_path)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        run_fake_renode(tmp_path, task, "ok", journal=jr)
    events = [json.loads(line) for line in jpath.read_text("utf-8").splitlines()]
    kinds = [e["kind"] for e in events]
    assert "task_start" in kinds and "task_result" in kinds
    assert events[-1]["passed"] is True


def test_renode_without_cmd_builds_wsl_pipeline(tmp_path, monkeypatch):
    # без renode_cmd: прошивка и стейдж уходят в WSL, затем запускается wsl-run.sh
    task = make_renode_task(tmp_path)
    runs, popens = [], []

    def fake_run(cmd, **kw):
        import subprocess

        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"FW-PUSHED\nSTAGE-PUSHED\n", stderr=b"")

    def fake_popen(cmd, **kw):
        popens.append(cmd)
        raise FileNotFoundError("wsl")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    res = run_task(task, out_dir=tmp_path / "out")
    assert not res.passed
    assert "не найден" in (res.error or "")
    assert len(runs) == 2  # прошивка + стейдж
    assert "ironharness-firmware" in runs[0][-1]
    assert "wsl-run.sh" in popens[0][-1]
    assert popens[0][:3] == ["wsl", "-d", "OpenClawGateway"]


def test_wsl_run_script_binds_firmware_from_home_store(tmp_path):
    # вся сложная логика — в wsl-run.sh (файлом, не через argv wsl.exe)
    task = make_renode_task(tmp_path)
    script = runner_module._wsl_run_script(task)
    assert "sleep infinity" in script  # монитор без вечного stdin получает EOF
    assert "> run.log 2>&1" in script  # вывод Renode не забивает pipe раннера
    assert "pkill -9 -f 'renode/renode|renode_[0-9]'" in script  # -9: mono медленно умирает от TERM
    assert 'echo "RENODE_PORT=$PORT"' in script  # динамический порт передаётся раннеру
    assert "CreateServerSocketTerminal $PORT" in script  # порт подставляет sed
    assert "$HOME/ironharness-firmware/fake.elf" in script
    # sed не должен оказаться в bash-комментарии (бывает при склейке строк)
    sed_line = next(l for l in script.splitlines() if "sed -i" in l)
    assert sed_line.strip().startswith("sed")


def test_wsl_cmd_is_simple_pipeline():
    cmd = runner_module._wsl_renode_cmd("$HOME/ironharness-runs/fake-rn")
    assert cmd[:3] == ["wsl", "-d", "OpenClawGateway"]
    assert cmd[-1] == "bash $HOME/ironharness-runs/fake-rn/wsl-run.sh"


def test_push_firmware_sends_tar_with_marker(tmp_path, monkeypatch):
    task = make_renode_task(tmp_path)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw.get("input")
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, stdout=b"FW-PUSHED\n", stderr=b"")

    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    runner_module._push_firmware(task)
    assert "ironharness-firmware" in seen["cmd"][-1]
    assert seen["input"][:2] == b"\x1f\x8b"  # gzip-магия tar.gz
    import io

    with runner_module.tarfile.open(fileobj=io.BytesIO(seen["input"])) as tar:
        assert tar.getnames() == ["fake.elf"]


def test_push_firmware_missing_is_value_error(tmp_path):
    task = make_renode_task(tmp_path)
    (task.directory / "fake.elf").unlink()
    with pytest.raises(ValueError, match="fake.elf"):
        runner_module._push_firmware(task)


def test_read_port_line():
    import io

    port = runner_module._read_port_line(io.BytesIO(b"RENODE_PORT=12345\n"), timeout=1)
    assert port == 12345
    with pytest.raises(ConnectionError, match="не сообщил порт"):
        runner_module._read_port_line(io.BytesIO(b""), timeout=0.2)


def test_is_infra_error_marks():
    assert is_infra_error("не найден: wokwi-cli")
    assert is_infra_error("мишень 'real' не реализована (этап 3)")
    assert is_infra_error("не удалось подключиться: :3456")
    assert is_infra_error("не удалось подготовить задачу: нет файла")
    assert not is_infra_error(None)
    # ошибка самой прошивки — не инфраструктурная, попытки продолжаются
    assert not is_infra_error("проверка не пройдена: ошибка в коде прошивки")
