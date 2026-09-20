"""Тесты real-мишени: живая плата эмулируется фейковым REPL-транспортом.

FakeBoard повторяет семантику MicroPython-REPL на ESP32: raw-paste (Ctrl+E код
Ctrl+D) запускает «прошивку» uart-echo; каждая строка ввода после старта
отвечает "echo: <строка>". read() с пустым буфером возвращает b"" — как
SerialTransport по таймауту. Железо в тестах не участвует (sim-before-real).
"""

import json
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
        self._acc = ""

    def _emit_bytes(self, data: bytes) -> None:
        with self._lock:
            self._buf += data

    def _emit(self, text: str) -> None:
        self._emit_bytes(text.encode("utf-8"))

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", "replace")
        if "\x03" in text:
            self._acc = ""
            self._paste_mode = False
            self._paste_buf = ""
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
        # a REPL executes on complete lines - accumulate until "\n", so a
        # 24-byte write chunk cannot split a probe statement in half; a bare
        # "\r" (Enter) completes a line too
        self._acc += text
        while "\n" in self._acc:
            line, _, self._acc = self._acc.partition("\n")
            self._handle_line(line.rstrip("\r"))
        if self._acc.endswith("\r"):
            line = self._acc
            self._acc = ""
            self._handle_line(line.rstrip("\r"))
        return len(data)

    def _handle_line(self, line: str) -> None:
        if "os.remove" in line and "main.py" in line:
            self._started = False  # гигиена: прошивка прошлой задачи снесена
            self.main_py = None
            return
        if self._paste_mode:
            # the REPL does not execute paste lines - the probe branch below
            # must not fire while paste mode is on
            if not self._paste_buf:
                self._emit("=== ")
            self._paste_buf += line
            self._emit(line + "\r\n")
            return
        if "binascii.hexlify" in line:
            # IH-79 backup probe: the colon-free probe lines answer per
            # main.py presence
            if self.main_py is None:
                self._emit("IH-BACKUP-ABSENT\r\n")
            else:
                import binascii

                self._emit("IH-BACKUP " + binascii.hexlify(self.main_py).decode() + "\r\n")
            return
        if self._paste_mode:
            if not self._paste_buf:  # the paste prompt precedes the first echoed line
                self._emit("=== ")
            self._paste_buf += line  # raw-paste keeps the source...
            # ...but legacy paste (live ESP32, IH-33 verification) ECHOES it back
            self._emit(line + "\r\n")
            return
        if self._started and line:
            self._emit(f"echo: {line}\r\n")

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            chunk = self._buf[:size]
            del self._buf[:size]
        return bytes(chunk)

    def close(self) -> None:
        self.closed = True


def make_real_task(tmp_path, entry=ECHO_ENTRY, stimulus=(), expect=("echo ready",), fail=()):
    d = tmp_path / "t"
    d.mkdir(exist_ok=True)
    # json.dumps, not repr(): the line is parsed as a YAML double-quoted
    # scalar, and repr's backslash doubling would turn a regex like
    # SCAN=\[118\] into a double-backslash literal that matches nothing
    text = "name: fake\ntarget: real\ntimeout_sec: 2\nentry: main.py\nexpect:\n" + "".join(
        f"  - {json.dumps(p)}\n" for p in expect
    )
    if fail:
        text += "fail:\n" + "".join(f"  - {json.dumps(p)}\n" for p in fail)
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


class EchoingBoard(FakeBoard):
    """IH-79 adversarial: a board that echoes every cooked-REPL byte back
    (live-verified behavior, IH-33) - the backup probe's own literals light
    up in the serial text before any real answer."""

    def write(self, data: bytes) -> int:
        self._emit(data.decode("utf-8", "replace"))
        return super().write(data)


def test_real_run_backs_up_main_py_with_echoing_board(tmp_path):
    # the decisive test: the probe literals are split in the source, so the
    # board's own echo cannot be mistaken for the backup answer
    task = make_real_task(tmp_path)
    res = run_task(task, out_dir=tmp_path / "out", real_transport=EchoingBoard())
    assert res.passed
    backups = list((tmp_path / "out").rglob("main.py.backup"))
    assert len(backups) == 1, "the previous main.py must be saved into run artifacts"
    assert backups[0].read_bytes() == b"print('old firmware')\n"


def test_backup_main_waits_for_full_answer_line(tmp_path):
    # IH-79 review: the answer line trickles in at line rate - parsing it
    # mid-line saved a truncated backup with status saved (fromhex accepts
    # an even-length prefix). The tail arrives in REAL TIME (threading
    # Timer): a read()-bound split would be drained by a single _pump and
    # never cross the poll boundary.
    import threading

    main_py = b"ab" * 300 + b"cd" * 10  # 620 hex chars - a real-size line

    class Trickle(FakeBoard):
        def __init__(self):
            super().__init__()
            self.main_py = main_py
            self._probe = b""
            self._answered = False

        def write(self, data):
            import binascii

            self._probe += data
            if b"UP-ABSENT')" in self._probe and not self._answered:
                self._answered = True
                answer = b"IH-BACKUP " + binascii.hexlify(self.main_py) + b"\r\n"
                half = len(answer) // 2
                self._emit_bytes(answer[:half])
                threading.Timer(0.4, self._emit_bytes, args=(answer[half:],)).start()
            return len(data)

    repl = RealRepl(Trickle())
    out = tmp_path / "art"
    status = repl._backup_main(out)
    assert status == "saved"
    assert (out / "main.py.backup").read_bytes() == main_py


def test_real_run_backs_up_main_py(tmp_path):
    task = make_real_task(tmp_path)
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeBoard())
    assert res.passed
    backups = list((tmp_path / "out").rglob("main.py.backup"))
    assert len(backups) == 1, "the previous main.py must be saved into run artifacts"
    assert backups[0].read_bytes() == b"print('old firmware')\n"


def test_real_run_without_main_py_reports_absent(tmp_path):
    # non-vacuous: the journal must record status=absent (the gate decision)
    from io_core.journal import JsonlJournal

    class NoMainBoard(FakeBoard):
        def __init__(self):
            super().__init__()
            self.main_py = None

    task = make_real_task(tmp_path)
    journal = JsonlJournal(tmp_path / "journal.jsonl", actor="test")
    try:
        res = run_task(task, out_dir=tmp_path / "out", real_transport=NoMainBoard(), journal=journal)
    finally:
        journal.close()
    assert res.passed
    events = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text("utf-8").splitlines()]
    backup = [e for e in events if e.get("kind") == "main_backup"]
    assert backup and backup[-1]["status"] == "absent"
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


def test_run_named_real_task_requires_allow_real_too(tmp_path, capsys, monkeypatch):
    # audit B (supersedes the IH-79 review nit): the gate keys on the TASK
    # target, not the flag combination - a named real task is gated exactly
    # like the bulk run (it wipes main.py all the same)
    from ironbench.cli import main as cli_main

    (tmp_path / "tasks").mkdir()
    make_real_task(tmp_path / "tasks")
    # a port that does not exist: the gate must pass without touching any
    # hardware (the run then infra-fails on the missing port)
    monkeypatch.setenv("IRONBENCH_REAL_PORT", "COM_NOPE")
    argv = [
        "run",
        "--task",
        "fake",
        "--tasks-dir",
        str(tmp_path / "tasks"),
        "--out",
        str(tmp_path / "out"),
    ]
    rc = cli_main(argv)
    assert rc == 2
    assert "refused" in capsys.readouterr().out
    # with the opt-in the gate passes (no board attached: infra, not refusal)
    rc = cli_main([*argv, "--allow-real"])
    assert rc != 2
    assert "refused" not in capsys.readouterr().out


def test_solve_native_real_task_requires_allow_real(tmp_path, capsys, monkeypatch):
    # audit B: the exact acceptance - a solve on a native real task with the
    # port set refuses with rc 2 BEFORE any LLM call
    from ironbench.cli import main as cli_main

    (tmp_path / "tasks").mkdir()
    make_real_task(tmp_path / "tasks")
    monkeypatch.setenv("IRONBENCH_REAL_PORT", "COM6")
    rc = cli_main(
        [
            "solve",
            "--task",
            "fake",
            "--tasks-dir",
            str(tmp_path / "tasks"),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "refused" in out and "--allow-real" in out


def test_solve_with_allow_real_passes_the_gate(tmp_path, capsys, monkeypatch):
    from ironbench.cli import main as cli_main

    (tmp_path / "tasks").mkdir()
    make_real_task(tmp_path / "tasks")
    monkeypatch.setenv("IRONBENCH_REAL_PORT", "COM6")
    called = {}

    def fake_results(task, cfg, *, attempts, solve_dir, allow_real=False):
        called["allow_real"] = allow_real
        return []

    monkeypatch.setattr("ironbench.cli.agent_solve_results", fake_results)
    rc = cli_main(
        [
            "solve",
            "--task",
            "fake",
            "--allow-real",
            "--tasks-dir",
            str(tmp_path / "tasks"),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc != 2, "the opt-in must pass the CLI gate"
    assert called.get("allow_real") is True


def test_solve_target_override_to_real_requires_allow_real(tmp_path, capsys, monkeypatch):
    # audit B review: the --target real OVERRIDE is the same gated path as a
    # native real task (same check, not pinned separately before)
    from ironbench.cli import main as cli_main

    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "n").mkdir()
    (tmp_path / "tasks" / "n" / "task.yaml").write_text(
        "name: n\nexpect:\n  - 'ready'\n", encoding="utf-8"
    )
    (tmp_path / "tasks" / "n" / "main.py").write_text("print('ready')\n", encoding="utf-8")
    monkeypatch.setenv("IRONBENCH_REAL_PORT", "COM_NOPE")
    rc = cli_main(
        [
            "solve",
            "--task",
            "n",
            "--target",
            "real",
            "--tasks-dir",
            str(tmp_path / "tasks"),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "refused" in out and "--allow-real" in out


# --- paid lesson 2026-09-20: the cooked REPL auto-indents after a colon ---


def test_cooked_mode_writes_never_open_a_block():
    """The live board (2026-09-20): the MicroPython cooked REPL auto-indents
    after a colon line; a hand-indented multi-line block never executes and
    the board sits in the line editor where Ctrl+D/Ctrl+E are dead - staging
    died with "did not enter paste mode". Every cooked-mode write in realhw
    (REMOVE_MAIN, the backup probe) must stay colon-free, so each line
    executes immediately at the prompt. Staging code is exempt: it is pasted
    inside paste mode, where no auto-indent exists."""
    assert b":" not in realhw.REMOVE_MAIN
    for line in realhw._BACKUP_PROBE_LINES:
        assert b":" not in line, (
            "a colon in a cooked-mode write opens a REPL block the board "
            "never leaves (live failure 2026-09-20)"
        )


def test_backup_main_survives_block_sinking_repl(tmp_path):
    """Behavioral pin: a REPL that sinks into block mode on the first colon
    byte (the live quirk) must still answer the probe, because the shipped
    probe opens no block. The fake swallows every cooked chunk after a colon
    until Ctrl+C - exactly what the live board did to the old try/except."""

    class BlockSinkBoard(FakeBoard):
        def __init__(self):
            super().__init__()
            self._sunk = False

        def write(self, data: bytes) -> int:
            text = data.decode("utf-8", "replace")
            if "\x03" in text:
                self._sunk = False
            elif not self._paste_mode and ":" in text:
                self._sunk = True  # block mode: nothing executes anymore
            if self._sunk:
                return len(data)  # swallowed - no echo, no execution
            return super().write(data)

    repl = RealRepl(BlockSinkBoard())
    out = tmp_path / "art"
    status = repl._backup_main(out)
    assert status == "saved"
    assert (out / "main.py.backup").read_bytes() == b"print('old firmware')\n"


def test_full_boot_survives_block_sinking_repl(tmp_path):
    """The BlockSink quirk through the whole boot(): interrupt, probe,
    REMOVE_MAIN, paste-mode staging. Review nit: _backup_main alone proved
    the probe; this proves the staged firmware still runs end to end when
    the editor would sink on any colon outside paste mode."""

    class BlockSinkBoard(FakeBoard):
        def __init__(self):
            super().__init__()
            self._sunk = False

        def write(self, data: bytes) -> int:
            text = data.decode("utf-8", "replace")
            if "\x03" in text:
                self._sunk = False
            elif not self._paste_mode and ":" in text:
                self._sunk = True  # block mode: nothing executes anymore
            if self._sunk:
                return len(data)  # swallowed - no echo, no execution
            return super().write(data)

    repl = RealRepl(BlockSinkBoard())
    out = tmp_path / "art"
    try:
        status = repl.boot("print('staged')\n", deadline=time.monotonic() + 30, backup_dir=out)
        assert status == "saved"
        assert (out / "main.py.backup").read_bytes() == b"print('old firmware')\n"
        # FakeBoard runs freshly staged code as its documented "echo ready"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "echo ready" not in repl.output():
            time.sleep(0.05)
            repl._drain()
        assert "echo ready" in repl.output(), "staged firmware must run to completion"
    finally:
        repl.close()


# --- IH-91/93: bus-diagnose - degradation diagnosis on the live bench ---


class FakeDiagBoard(FakeBoard):
    """A REPL running the bus-diagnose protocol: "bus ready" at boot, one
    diagnosis round per received line. The emulated bus answers [60, 118]
    - both expected devices (BME280 at 0x76, OLED at 0x3C) present, the
    healthy state the golden's expects pin since IH-93. Modes model the
    firmware kinds the bench must tell apart: honest (scans and reports),
    boot_dumps (prints the whole diagnosis at boot and ignores input - the
    stamp anchor must condemn it), degraded_hardcode (reports the degraded
    bus - the prompt never says WHICH state the bus is in, so firmware
    that guesses instead of scanning fails on the missed expect), crasher
    (dies at boot with the station's unhandled ENODEV traceback - the
    fail patterns must fire), silent (answers nothing - an honest miss,
    no anti-cheat verdict).
    """

    BOOT = "bus ready\r\n"
    TRUTH = "SCAN=[60, 118] MISSING=none\r\nSTATUS=ok\r\n"
    WRONG = "SCAN=[118] MISSING=0x3C\r\nSTATUS=degraded\r\n"
    CRASH = (
        "Traceback (most recent call last):\r\n"
        '  File "main.py", line 4, in <module>\r\n'
        "OSError: [Errno 19] ENODEV\r\n"
    )

    def __init__(self, mode: str = "honest") -> None:
        super().__init__()
        self._mode = mode

    def _answer(self) -> None:
        if self._mode == "degraded_hardcode":
            self._emit(self.WRONG)
        else:
            self._emit(self.TRUTH)

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", "replace")
        if "\x04" in text and self._paste_mode and self._paste_buf:
            self._paste_mode = False
            self._paste_buf = ""
            if self._mode == "crasher":
                self._emit(self.BOOT + self.CRASH)
                return len(data)
            self._started = True
            self._emit(self.BOOT)
            if self._mode == "boot_dumps":
                self._emit(self.TRUTH)
            return len(data)
        if (
            self._started
            and not self._paste_mode
            and "\x03" not in text
            and "main.py" not in text
            and self._mode not in ("boot_dumps", "silent")
        ):
            if any(line.strip() for line in text.splitlines()):
                self._answer()
            return len(data)
        return super().write(data)


DIAG_TASK_KWARGS = {
    "entry": (
        "print('bus ready')\n"
        "while True:\n"
        "    if not input().strip():\n"
        "        continue\n"
        "    print('SCAN=[60, 118] MISSING=none')\n"
        "    print('STATUS=ok')\n"
    ),
    "stimulus": ('write-serial: "diag\\n"', 'wait-serial: "MISSING="'),
    "expect": ("bus ready", r"SCAN=\[60, 118\]", "MISSING=none", "STATUS=ok"),
    "fail": ("Traceback", "ENODEV"),
}


def test_real_target_golden_bus_diagnose_passes(tmp_path):
    task = make_real_task(tmp_path, **DIAG_TASK_KWARGS)
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeDiagBoard())
    assert res.passed, res.missed or res.error


def test_bus_diagnose_boot_dump_cheater_fails(tmp_path):
    # the diagnosis printed at boot (before the "diag" request) trips the
    # IH-14 chunk-stamp anchor: a pre-printed answer is a cheat verdict
    # (error_kind "run"), so solve keeps iterating
    task = make_real_task(tmp_path, **DIAG_TASK_KWARGS)
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeDiagBoard(mode="boot_dumps"))
    assert not res.passed
    assert "anti-cheat" in (res.error or "")
    assert res.error_kind == "run"
    assert not is_infra_error(res)


def test_bus_diagnose_degraded_guess_fails(tmp_path):
    # the prompt names the EXPECTED device set only - firmware that guesses
    # the degraded answer instead of scanning the bus misses the pinned
    # healthy expects (this is what makes the task a diagnosis, not a
    # copy-from-description exercise)
    task = make_real_task(tmp_path, **DIAG_TASK_KWARGS)
    res = run_task(
        task, out_dir=tmp_path / "out", real_transport=FakeDiagBoard(mode="degraded_hardcode")
    )
    assert not res.passed
    assert res.error is None  # honest miss, not a cheat verdict
    assert "MISSING=none" in res.missed
    assert "STATUS=ok" in res.missed


def test_bus_diagnose_crasher_fails_on_fail_patterns(tmp_path):
    # the real station's failure mode: firmware inits the absent OLED, the
    # unhandled ENODEV traceback kills main.py - the fail patterns must
    # fire, not merely the missed expects
    task = make_real_task(tmp_path, **DIAG_TASK_KWARGS)
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeDiagBoard(mode="crasher"))
    assert not res.passed
    assert res.hit_fail == ("Traceback", "ENODEV")


def test_bus_diagnose_no_answer_is_honest_miss(tmp_path):
    task = make_real_task(tmp_path, **DIAG_TASK_KWARGS)
    res = run_task(task, out_dir=tmp_path / "out", real_transport=FakeDiagBoard(mode="silent"))
    assert not res.passed
    assert res.error is None
    assert any("MISSING=" in m for m in res.missed)


def test_make_real_task_roundtrips_regex_backslashes(tmp_path):
    # the class fix (IH-91): the helper's pattern lines are YAML double-quoted
    # scalars built with json.dumps - a regex with backslashes must reach
    # load_task intact (repr doubled them into a non-matching literal; found
    # while authoring bus-diagnose, whose SCAN pattern needs \[)
    task = make_real_task(tmp_path, expect=(r"SCAN=\[118\]", r"T=\d\.\d"))
    assert task.expect == (r"SCAN=\[118\]", r"T=\d\.\d")


# --- audit B (2026-09-20): no wipe without a verified backup ---


def test_boot_refuses_wipe_without_verified_backup(tmp_path):
    """The probe got no parseable answer (status unknown) - boot must refuse
    before any destructive write: user firmware it never captured must not
    be removed on a guess."""

    class MuteProbeBoard(FakeBoard):
        def _handle_line(self, line):
            if "binascii.hexlify" in line:
                return  # the probe is never answered: status unknown
            super()._handle_line(line)

    repl = RealRepl(MuteProbeBoard())
    out = tmp_path / "art"
    seen = bytearray()
    original_write = repl.write

    def spy(data):
        seen.extend(data)
        original_write(data)

    repl.write = spy
    try:
        with pytest.raises(ConnectionError, match="refusing to wipe main.py"):
            repl.boot("print('x')\n", deadline=time.monotonic() + 30, backup_dir=out)
    finally:
        repl.close()
    assert b"os.remove" not in seen, "REMOVE_MAIN ran without a verified backup"
    assert not (out / "main.py.backup").exists()


def test_probe_sliding_deadline_outlives_the_fixed_window(tmp_path):
    """audit B: the answer trickles in slowly - every arriving chunk extends
    the probe window (total-capped), so a slow board is not misread as
    'unknown' (which would now refuse the whole staging)."""
    import binascii
    import threading

    main_py = b"ab" * 200
    answer = b"IH-BACKUP " + binascii.hexlify(main_py) + b"\r\n"

    class SlowTrickle(FakeBoard):
        def __init__(self):
            super().__init__()
            self.main_py = main_py
            self._probe = b""
            self._sent = 0

        def write(self, data):
            self._probe += data
            if b"binascii.hexlify" in self._probe and not self._sent:
                self._sent = 1
                chunks = [answer[i : i + 40] for i in range(0, len(answer), 40)]
                for delay, chunk in enumerate(chunks, start=1):
                    threading.Timer(0.4 * delay, self._emit_bytes, args=(chunk,)).start()
            return len(data)

    repl = RealRepl(SlowTrickle())
    out = tmp_path / "art"
    try:
        status = repl._backup_main(out)
    finally:
        repl.close()
    assert status == "saved", "the sliding probe window gave up on a slow board"
    assert (out / "main.py.backup").read_bytes() == main_py
