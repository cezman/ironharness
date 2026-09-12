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
from ironbench.runner import is_infra_error, run_task
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
    monkeypatch.setattr("ironbench.runner_common.WALL_GRACE_SEC", 0.5)


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
    assert res.error_kind == "infra"  # IH-22 pin: board I/O is environment-level


def test_real_target_preprinted_needle_is_run_not_infra(tmp_path):
    # IH-22 pin: the anti-cheat verdict on the real target is a property of the
    # agent's code (error_kind "run"), so solve must keep iterating instead of
    # exiting early as it would on an infra failure. The cheater is modeled at
    # the firmware level: the expected line prints right at code start (boot()
    # clears the buffer before staging, so a transport-level pre-print would
    # not reach the run output).
    class PrePrinter(FakeBoard):
        def write(self, data: bytes) -> int:
            if b"\x04" in data and self._paste_buf:  # Ctrl+D runs the staged code
                self._emit("echo: pre-hack\r\n")
            return super().write(data)

    task = make_real_task(
        tmp_path,
        stimulus=('write-serial: "go\\n"', 'wait-serial: "echo: pre-hack"'),
        expect=("echo ready",),
    )
    res = run_task(task, out_dir=tmp_path / "out", real_transport=PrePrinter())
    assert not res.passed
    assert "anti-cheat" in (res.error or "")
    assert res.error_kind == "run"
    assert not is_infra_error(res)


def test_task_yaml_roundtrip():
    # stimulus с write-serial из yaml доходит до раннера как словарь
    raw = yaml.safe_load('stimulus:\n  - write-serial: "hi\\n"')
    assert raw["stimulus"][0] == {"write-serial": "hi\n"}


# --- IH-33: the bme-read protocol on the stamp anchor ---


class FakeBmeBoard(FakeBoard):
    """A REPL running the bme-read protocol: "bme ready" at boot, one reading
    line per received non-empty line. answer_delay=0 emits synchronously in
    write() - like a fast honest board whose answer is fully emitted before
    the runner's next pump (the race the presence-based check used to
    condemn); answer_delay>0 emits from a timer thread (the wait loop path).
    boot_dumps=True models the cheater firmware: the reading is printed right
    at boot and further input is ignored. answers=False models firmware that
    never answers (an honest miss, no anti-cheat verdict).
    """

    READING = "T=22.71 C P=1008.3 hPa H=41.2 %"

    def __init__(self, answer_delay: float = 0.0, boot_dumps: bool = False, answers: bool = True) -> None:
        super().__init__()
        self._answer_delay = answer_delay
        self._boot_dumps = boot_dumps
        self._answers = answers

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", "replace")
        if "\x04" in text and self._paste_mode and self._paste_buf:
            # Ctrl+D runs the staged bme firmware: boot line (+ the cheater's dump)
            self._paste_mode = False
            self._paste_buf = ""
            self._started = True
            boot = "bme ready\r\n" + (self.READING + "\r\n" if self._boot_dumps else "")
            self._emit(boot)
            return len(data)
        if (
            self._started
            and self._answers
            and not self._paste_mode
            and "\x03" not in text
            and "main.py" not in text
        ):
            for line in text.splitlines():
                if line.strip():
                    if self._answer_delay:
                        threading.Timer(self._answer_delay, self._emit, (self.READING + "\r\n",)).start()
                    else:
                        self._emit(self.READING + "\r\n")
            return len(data)
        return super().write(data)


BME_TASK_KWARGS = {
    "entry": (
        "print('bme ready')\n"
        "while True:\n"
        "    if not input().strip():\n"
        "        continue\n"
        "    print('" + FakeBmeBoard.READING + "')\n"
    ),
    "stimulus": ('write-serial: "read\\n"', 'wait-serial: "T="'),
    # readable literals double as regexes ('.' matches itself)
    "expect": ("bme ready", "T=22.71 C", "P=1008.3 hPa", "H=41.2 %"),
}


def test_real_target_golden_bme_read_passes(tmp_path):
    # The honest fast answer is fully emitted during the write (before the
    # wait step's first pump) and must be CREDITED: the chunk-stamp anchor
    # stamps it at ingestion, which happens after the trigger. The old
    # presence-based check condemned exactly this firmware (failed on main).
    task = make_real_task(tmp_path, **BME_TASK_KWARGS)
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeBmeBoard())
    assert res.passed, res.missed or res.error


def test_real_target_slow_answer_still_credited(tmp_path):
    task = make_real_task(tmp_path, **BME_TASK_KWARGS)
    res = run_task(
        task, out_dir=tmp_path / "out", real_transport=FakeBmeBoard(answer_delay=0.3)
    )
    assert res.passed, res.missed or res.error


def test_real_target_boot_dump_cheater_fails(tmp_path):
    # The cheater prints ready + constant readings at boot and never reads a
    # sensor (it has none): every expect pattern matches the whole log, but
    # the waited needle's first occurrence was ingested before the stimulus
    # write - the run verdict is "pre-printed output" (error_kind "run").
    task = make_real_task(tmp_path, **BME_TASK_KWARGS)
    res = run_task(
        task, out_dir=tmp_path / "out", real_transport=FakeBmeBoard(boot_dumps=True, answers=False)
    )
    assert not res.passed
    assert "anti-cheat" in (res.error or "")
    assert res.error_kind == "run"
    assert not is_infra_error(res)  # solve keeps iterating on a cheat verdict


def test_real_target_no_answer_is_honest_miss(tmp_path):
    # A firmware that answers nothing: no anti-cheat verdict, the waits just miss.
    task = make_real_task(tmp_path, **BME_TASK_KWARGS)
    res = run_task(
        task, out_dir=tmp_path / "out", real_transport=FakeBmeBoard(answers=False)
    )
    assert not res.passed
    assert res.error is None
    assert any("T=" in m for m in res.missed)
