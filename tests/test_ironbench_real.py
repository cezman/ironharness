"""Тесты real-мишени: живая плата эмулируется фейковым REPL-транспортом.

FakeBoard повторяет семантику MicroPython-REPL на ESP32: raw-paste (Ctrl+E код
Ctrl+D) запускает «прошивку» uart-echo; каждая строка ввода после старта
отвечает "echo: <строка>". read() с пустым буфером возвращает b"" — как
SerialTransport по таймауту. Железо в тестах не участвует (sim-before-real).
"""

import threading
import time

import pytest
import yaml

from ironbench import realhw
from ironbench.realhw import RealRepl
from ironbench.runner import run_task
from ironbench.tasks import load_task

ECHO_ENTRY = (
    'print("echo " + "ready")\nwhile True:\n    line = input()\n    print("echo: " + line.strip())\n'
)


class FakeBoard:
    """Duck-typed serial: emulates a MicroPython REPL running staged uart-echo code.

    Semantics mirrored from live hardware: os.remove('main.py') hygiene clears
    the staged firmware, Ctrl+E enters paste mode (no source echo), Ctrl+D runs
    the freshly staged code (prints "echo ready"), input() echoes are line-fed.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._paste_mode = False
        self._paste_buf = ""
        self._started = False
        self.closed = False

    def _emit(self, text: str) -> None:
        with self._lock:
            self._buf += text.encode("utf-8")

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", "replace")
        if "\x03" in text:
            return len(data)  # прерывание — ничего не печатаем
        if "main.py" in text and "os.remove" in text:
            self._started = False  # гигиена: прошивка прошлой задачи снесена
            return len(data)
        if "\x05" in text:
            self._paste_mode = True
            self._paste_buf = ""
            self._emit("\r\npaste mode; Ctrl-C to cancel, Ctrl-D to finish\r\n=== ")
            return len(data)
        if "\x04" in text:
            self._paste_mode = False
            if self._paste_buf:  # прогон свежезалитого кода
                self._started = True
                self._emit("echo ready\r\n")
                self._paste_buf = ""
            return len(data)
        if self._paste_mode:
            self._paste_buf += text  # raw-paste не эхолит исходник
            return len(data)
        if self._started:
            for line in text.splitlines():
                if line:
                    self._emit(f"echo: {line}\r\n")
        return len(data)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            chunk = self._buf[:size]
            del self._buf[:size]
        return bytes(chunk)

    def close(self) -> None:
        self.closed = True


def make_real_task(tmp_path, entry=ECHO_ENTRY, stimulus=(), expect=("echo ready",)):
    d = tmp_path / "t"
    d.mkdir(exist_ok=True)
    text = "name: fake\ntarget: real\ntimeout_sec: 2\nentry: main.py\nexpect:\n" + "".join(
        f"  - {p!r}\n" for p in expect
    )
    if stimulus:
        text += "stimulus:\n" + "\n".join(f"  - {s}" for s in stimulus) + "\n"
    (d / "task.yaml").write_text(text, encoding="utf-8")
    (d / "main.py").write_text(entry, encoding="utf-8")
    return load_task(d)


@pytest.fixture(autouse=True)
def fast_boot(monkeypatch):
    monkeypatch.setattr(realhw, "_BOOT_QUIET_SEC", 0.05)
    monkeypatch.setattr(realhw, "_SOFT_RESET_SEC", 0.05)
    monkeypatch.setattr("ironbench.runner.WALL_GRACE_SEC", 0.5)


def test_repl_boot_and_echo():
    board = FakeBoard()
    repl = RealRepl(board)
    repl.boot(ECHO_ENTRY)
    assert repl.wait_for("echo ready", time.monotonic() + 3)
    repl.write(b"hello\r")
    assert repl.wait_for("echo: hello", time.monotonic() + 3)
    repl.close()
    assert board.closed


def test_real_target_happy_path(tmp_path):
    task = make_real_task(
        tmp_path,
        stimulus=('write-serial: "hello\\n"', 'write-serial: "world\\n"'),
        expect=("echo ready", "echo: hello", "echo: world"),
    )
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeBoard())
    assert res.passed, res.missed or res.error
    assert res.exit_code is None  # живой платы как процесса нет
    assert res.serial_log is not None and res.serial_log.is_file()


def test_real_target_missed_expect_fails(tmp_path):
    task = make_real_task(
        tmp_path,
        stimulus=('write-serial: "hello\\n"',),
        expect=("echo ready", "echo: never-said"),
    )
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeBoard())
    assert not res.passed
    assert res.missed == ("echo: never-said",)


def test_real_target_unsupported_steps_are_honest_fails(tmp_path):
    task = make_real_task(tmp_path, stimulus=("set-control: ds18b20",))
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeBoard())
    assert not res.passed
    assert "set-control" in (res.error or "")


def test_real_target_transport_exception_is_infra(tmp_path):
    class Dead:
        def write(self, data):
            raise OSError("port gone")

        def read(self, size):
            raise OSError("port gone")

        def close(self):
            pass

    task = make_real_task(tmp_path)
    res = run_task(task, out_dir=tmp_path / "out", real_transport=Dead())
    assert not res.passed
    assert "failed to talk to the board" in (res.error or "")


def test_task_yaml_roundtrip():
    # stimulus с write-serial из yaml доходит до раннера как словарь
    raw = yaml.safe_load('stimulus:\n  - write-serial: "hi\\n"')
    assert raw["stimulus"][0] == {"write-serial": "hi\n"}
