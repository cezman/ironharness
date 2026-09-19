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
    the staged firmware, Ctrl+E enters paste mode, legacy paste echoes the
    staged source back (live-verified IH-33), Ctrl+D runs the freshly staged
    code (prints "echo ready"), input() echoes are line-fed.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._paste_mode = False
        self._paste_buf = ""
        self._started = False
        self.closed = False
        self.main_py = b"print('old firmware')\n"

    def _emit(self, text: str) -> None:
        with self._lock:
            self._buf += text.encode("utf-8")

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", "replace")
        if "\x03" in text:
            return len(data)  # прерывание — ничего не печатаем
        if "IH-BACKUP" in text:
            # IH-79: the backup probe - answer with the saved main.py
            if self.main_py is None:
                self._emit("IH-BACKUP-ABSENT\r\n")
            else:
                import binascii

                self._emit("IH-BACKUP " + binascii.hexlify(self.main_py).decode() + "\r\n")
            return len(data)
        if "main.py" in text and "os.remove" in text:
            self._started = False  # гигиена: прошивка прошлой задачи снесена
            self.main_py = None
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
            if not self._paste_buf:  # the paste prompt precedes the first echoed line
                self._emit("=== ")
            self._paste_buf += text  # raw-paste keeps the source...
            # ...but legacy paste (live ESP32, IH-33 verification) ECHOES it back
            self._emit(text)
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
    "stimulus": ('write-serial: "read\\n"', 'wait-serial: "hPa"'),
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


def test_real_pump_caps_retained_output():
    """IH-46: a board printing without pause grew _text/_chunks without bound
    (rate-bounded only by baud). Retained output is capped at MAX_SERIAL_TEXT
    and (text, chunks) stay position-consistent for the anticheat mapping."""
    from ironbench.runner_common import MAX_SERIAL_TEXT

    class FloodBoard:
        def __init__(self) -> None:
            self.left = 2 << 20  # 2 MB pending: twice the cap

        @property
        def in_waiting(self) -> int:
            return min(4096, self.left)

        def read(self, n: int) -> bytes:
            n = min(n, self.left)
            self.left -= n
            return b"A" * n

        def write(self, data: bytes) -> None:
            pass

    repl = RealRepl(FloodBoard(), boot_quiet_sec=0)
    out = repl.output()
    assert len(out) <= MAX_SERIAL_TEXT + 65536, (
        f"the real pump retained {len(out)} bytes - no cap"
    )
    assert "".join(c for _, c in repl.chunks()) == out
    assert "truncated" in out, "the cap was hit silently - no truncation marker (IH-46)"


def test_real_pump_decodes_split_multibyte_chars():
    """IH-58 (real side): _pump decoded per read() chunk - a multibyte char
    split by a chunk boundary became two U+FFFD and broke honest non-ASCII
    firmware output ('20°C' split mid-degree)."""

    class Utf8FloodBoard:
        def __init__(self) -> None:
            self.left = b"result: 20\xc2\xb0 C ok"

        @property
        def in_waiting(self) -> int:
            return min(11, len(self.left))  # the 11-byte read cuts between 0xc2 and 0xb0

        def read(self, n: int) -> bytes:
            take = min(n, len(self.left))
            out = self.left[:take]
            self.left = self.left[take:]
            return out

        def write(self, data: bytes) -> None:
            pass

    repl = RealRepl(Utf8FloodBoard(), boot_quiet_sec=0)
    out = repl.output()
    assert out == "result: 20° C ok", repr(out)
    assert "\ufffd" not in out, f"replacement chars in output: {out!r}"


def test_real_boot_respects_the_wall_deadline(tmp_path, monkeypatch):
    """IH-57: boot() wrote line by line with fixed per-chunk delays and no
    deadline - an oversized agent file stalled the attempt far past the wall
    clock. The staging must abort as a timeout at the deadline."""
    import ironbench.realhw as realhw_mod
    from ironbench import runner_common

    monkeypatch.setattr(realhw_mod, "_WRITE_CHUNK_DELAY", 0.1)
    monkeypatch.setattr(runner_common, "WALL_GRACE_SEC", 1)
    monkeypatch.setattr(runner_common, "WALL_GRACE_SEC", 1)
    entry = "".join(f"print({i})\n" for i in range(60)) + 'print("echo ready")\n'
    task = make_real_task(tmp_path, entry=entry, expect=("echo ready",))

    started = time.monotonic()
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeBoard())
    duration = time.monotonic() - started
    assert res.error_kind == "timeout", f"got {res.error_kind}: {res.error}"
    assert duration < 9, f"the staging ran {duration:.1f}s past the wall deadline"


LITERAL_ENTRY = 'print("echo ready")\nwhile True:\n    line = input()\n    print("echo: " + line)\n'


class TruncatedEchoBoard(FakeBoard):
    """A degraded link: only the FIRST pasted line is echoed - the echo's
    tail (including the last source line) is lost (IH-56 F5)."""

    def __init__(self) -> None:
        super().__init__()
        self._echoed_first = False

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", "replace")
        if self._paste_mode and "\x04" not in text and "\x05" not in text:
            if not self._echoed_first:
                self._echoed_first = True
                return super().write(data)
            self._paste_buf += text  # consume silently: the echo tail is lost
            return len(data)
        return super().write(data)


class DeadTruncatedEchoBoard(TruncatedEchoBoard):
    """Truncated echo + the board never runs the staged code (dies at
    Ctrl+D): with the trim disabled this board PASSED via its own echo."""

    def write(self, data: bytes) -> int:
        if "\x04" in data.decode("utf-8", "replace"):
            self._paste_mode = False
            self._paste_buf = ""
            return len(data)  # no start, no output
        return super().write(data)


def test_truncated_staging_echo_refuses_instead_of_condemning(tmp_path):
    """IH-56 F5a: with the echo's tail lost, the untrimmable echo used to
    stay in the log - an honest board was condemned by the anti-cheat
    (pre-trigger needle from its own echo). The boot must refuse instead:
    infra, not a run verdict against honest firmware."""
    task = make_real_task(tmp_path, entry=LITERAL_ENTRY, expect=("echo ready",))
    res = run_task(task, out_dir=tmp_path / "out", real_transport=TruncatedEchoBoard())
    assert not res.passed
    assert res.error_kind == "infra", f"got {res.error_kind}: {res.error}"
    assert "truncated" in (res.error or "")


def test_truncated_echo_dead_board_cannot_pass(tmp_path):
    """IH-56 F5b: the retained echo contains the expect literal - a board
    that never executed anything used to PASS from its own echo."""
    task = make_real_task(tmp_path, entry=LITERAL_ENTRY, expect=("echo ready",))
    res = run_task(task, out_dir=tmp_path / "out", real_transport=DeadTruncatedEchoBoard())
    assert not res.passed, "a board that never ran passed from its own paste echo"
    assert res.error_kind == "infra"


# --- IH-79: main.py belongs to the user - it is backed up before the wipe ---


def test_real_run_backs_up_main_py(tmp_path):
    task = make_real_task(tmp_path)
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeBoard())
    assert res.passed
    backups = list((tmp_path / "out").rglob("main.py.backup"))
    assert len(backups) == 1, "the previous main.py must be saved into run artifacts"
    assert backups[0].read_bytes() == b"print('old firmware')\n"


def test_real_run_without_main_py_skips_backup(tmp_path):
    task = make_real_task(tmp_path)

    class EmptyBoard(FakeBoard):
        def __init__(self):
            super().__init__()
            self.main_py = None

    res = run_task(task, out_dir=tmp_path / "out", real_transport=EmptyBoard())
    assert res.passed
    assert not list((tmp_path / "out").rglob("main.py.backup"))


def test_run_all_refuses_real_tasks_without_allow_real(tmp_path, capsys):
    # IH-79: --all is bulk; a wrong IRONHARNESS_REAL_PORT would wipe someone
    # else's main.py - the bulk wipe is an explicit opt-in
    from ironbench.cli import main as cli_main

    (tmp_path / "tasks").mkdir()
    make_real_task(tmp_path / "tasks")
    rc = cli_main(
        [
            "run",
            "--all",
            "--tasks-dir",
            str(tmp_path / "tasks"),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "--allow-real" in out and "wipes main.py" in out
