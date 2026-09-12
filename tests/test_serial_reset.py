"""serial_reset tests (IH-19): an explicit RTS pulse resets the board
(ESP32 wiring: RTS->EN, DTR->IO0). The idle-lines contract is the inverse
pin: open() must NOT reset (commit 9e836d7), reset happens only on an
explicit call. Loop ports accept the line assignments as no-ops, so the
sequence is verified against a tracking fake; journaling and the
reader-interplay are verified through Session.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from io_core.journal import read_events
from io_core.serial_transport import SerialTransport
from io_core.session import Session


class TrackingLines:
    """Mimics a pyserial port with modem lines plus a read that never ends
    until the port closes - models a live link for lock-interplay checks."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self._closed = False

    @property
    def dtr(self) -> bool:
        return False

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self.log.append(f"dtr={value}")

    @property
    def rts(self) -> bool:
        return False

    @rts.setter
    def rts(self, value: bool) -> None:
        self.log.append(f"rts={value}")

    def read(self, size: int) -> bytes:
        # block "forever" in small sleeps so we can observe lock interplay
        while not self._closed:
            time.sleep(0.02)
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self._closed = True


def test_reset_pulses_rts_with_dtr_low():
    t = SerialTransport("loop://", timeout=0.05)
    t.open()
    inner = TrackingLines()
    t._serial = inner  # observe the exact line sequence
    t.reset(pulse_sec=0.01, settle_sec=0.01)
    assert inner.log == ["dtr=False", "rts=True", "rts=False"]
    t.close()


def test_open_sets_idle_lines_and_never_resets(monkeypatch):
    # The idle-lines pin, full form: open() sets DTR/RTS idle ONCE (9e836d7)
    # and no reset pulse may happen implicitly - a reset is only ever an
    # explicit call. serial_for_url is redirected to the tracking fake, so
    # even the idle set is observed: exactly the two idle assignments.
    inner = TrackingLines()
    monkeypatch.setattr(
        "io_core.serial_transport.serial.serial_for_url", lambda *a, **k: inner
    )
    t = SerialTransport("loop://", timeout=0.05)
    t.open()
    assert inner.log == ["dtr=False", "rts=False"]
    t.close()
    assert inner.log == ["dtr=False", "rts=False"]  # close adds no churn either


def test_reset_journaled_with_conn(tmp_path: Path):
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("board", "loop://", timeout=0.05)
        s.serial_reset("board", pulse_sec=0.01, settle_sec=0.01)
    finally:
        s.close()
    events = read_events(tmp_path / "j.jsonl")
    resets = [e for e in events if e["kind"] == "reset"]
    assert resets and resets[-1]["conn"] == "board"
    assert resets[-1]["pulse_sec"] == 0.01


def test_reset_under_reader_lock_and_boot_output_flows(tmp_path: Path):
    # reset while a reader runs: the line control goes through the reader's
    # I/O lock (single line!), and the reader stays alive to catch boot output
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("loop", "loop://", timeout=0.05)
        s.serial_reader_start("loop")
        s.serial_reset("loop", pulse_sec=0.01, settle_sec=0.01)
        assert s.serial_tail("loop")["alive"] is True
        s.serial_write("loop", b"after-reset".hex())
        got = s.serial_read_until("loop", "after-reset", timeout=5)
        assert got["found"] is True
        s.serial_reader_stop("loop")
        s.serial_close("loop")
    finally:
        s.close()
    events = read_events(tmp_path / "j.jsonl")
    kinds = [e["kind"] for e in events]
    assert "reset" in kinds and "reader_start" in kinds


def test_reset_without_port_is_keyerror(tmp_path: Path):
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        with pytest.raises(KeyError, match="not open"):
            s.serial_reset("nope")
        s.serial_open("board", "loop://", timeout=0.05)
        s.serial_close("board")
        with pytest.raises(KeyError, match="not open"):
            s.serial_reset("board")
    finally:
        s.close()


def test_reset_failure_journaled_and_raises(tmp_path: Path):
    # A port without modem lines (pty, some USB hubs) refuses the ioctl:
    # the reset failure must be journaled (reset_failed) and raised - never
    # silently swallowed into a fake "ok".
    class NoLines:
        dtr = property(lambda self: False, lambda self, v: (_ for _ in ()).throw(OSError("no ioctl")))
        rts = property(lambda self: False, lambda self, v: (_ for _ in ()).throw(OSError("no ioctl")))

        def read(self, size: int) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None:
            pass

    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("board", "loop://", timeout=0.05)
        s._serial_base["board"]._serial = NoLines()
        with pytest.raises(OSError, match="no ioctl"):
            s.serial_reset("board", pulse_sec=0.01, settle_sec=0.01)
    finally:
        s.close()
    events = read_events(tmp_path / "j.jsonl")
    failed = [e for e in events if e["kind"] == "reset_failed"]
    assert failed and "no ioctl" in failed[-1]["error"]


def test_reset_serialized_against_reader_drain(tmp_path: Path):
    # CH340 single-line contract without hardware: while the reader drains
    # (blocking read inside io_lock), a concurrent serial_reset must PARK on
    # the reader's I/O lock - line control may never race a data read. The
    # negative assertion (reset does NOT finish while the reader holds the
    # lock) is the mechanism pin: without the io_lock in Session.serial_reset
    # the reset would complete immediately (mutation-verified).
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("board", "loop://", timeout=0.05)
        base = s._serial_base["board"]
        entered_read = threading.Event()
        release_read = threading.Event()

        class BlockingRead:
            dtr = None
            rts = None

            def read(self, size: int) -> bytes:
                entered_read.set()
                release_read.wait(timeout=10)
                return b""

            def write(self, data: bytes) -> int:
                return len(data)

            def close(self) -> None:
                release_read.set()

        base._serial = BlockingRead()
        s.serial_reader_start("board")
        assert entered_read.wait(timeout=5), "reader never started draining"
        reset_done = threading.Event()
        errors: list[BaseException] = []

        def resetter() -> None:
            try:
                s.serial_reset("board", pulse_sec=0.01, settle_sec=0.01)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
            reset_done.set()

        threading.Thread(target=resetter, daemon=True).start()
        # the reader holds io_lock inside its blocking read: the reset must wait
        assert not reset_done.wait(timeout=0.3), (
            "serial_reset did not wait for the reader's I/O lock "
            "(line control raced a data read)"
        )
        release_read.set()
        assert reset_done.wait(timeout=10), "reset deadlocked against the reader"
        assert errors == []
        s.serial_reader_stop("board")
        s.serial_close("board")
    finally:
        s.close()


def test_reset_settle_waits_for_the_boot(tmp_path: Path):
    # The settle promise ("waits settle_sec for the boot") needs a mechanism
    # test: the call must take at least pulse_sec + settle_sec of wall time.
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("board", "loop://", timeout=0.05)
        t0 = time.monotonic()
        s.serial_reset("board", pulse_sec=0.05, settle_sec=0.3)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.3, f"reset returned in {elapsed:.3f}s - no settle wait"
    finally:
        s.close()
