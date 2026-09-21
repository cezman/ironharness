"""IH-51/IH-52 hardening tail: gate refusals must journal (no log = didn't
happen covers refusals), and the two deadline-bounded-but-not-byte-bounded
accumulators (mprepl._Stream._buf, verify.expect_read) get byte caps.
"""

from __future__ import annotations

import threading
import time

import pytest

from io_core.errors import TransportIoError, VerificationError
from io_core.journal import read_events
from io_core.mprepl import _Stream
from io_core.session import Session
from io_core.verify import expect_read


def test_gate_refusals_are_journaled(tmp_path):
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("loop", "loop://")
        (tmp_path / "sb" / "src.txt").write_bytes(b"x")
        s.serial_reader_start("loop")
        with pytest.raises(RuntimeError):
            s.serial_read("loop")
        with pytest.raises(RuntimeError):
            s.serial_read_line("loop")
        (tmp_path / "sb" / "src.txt").write_bytes(b"x")
        with pytest.raises(RuntimeError):
            s.serial_put("loop", "src.txt", "dst.txt")
        with pytest.raises(RuntimeError):
            s.serial_get("loop", "main.py", "dump.py")
        with pytest.raises(KeyError):
            s.serial_reader_start("loop")
        ks = {e["kind"] for e in read_events(tmp_path / "j.jsonl")}
        for kind in (
            "read_refused",
            "read_line_refused",
            "put_refused",
            "get_refused",
            "reader_start_refused",
        ):
            assert kind in ks, f"the {kind} refusal passed unjournaled"
        s.serial_reader_stop("loop")
    finally:
        s.close()


def test_get_refused_during_transfer_is_journaled(tmp_path, monkeypatch):
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("loop", "loop://")
        (tmp_path / "sb" / "src.txt").write_bytes(b"x")
        release = threading.Event()
        started = threading.Event()

        def slow_put(t, data, target_path):
            started.set()
            release.wait(timeout=10)
            return len(data)

        from io_core import mprepl

        monkeypatch.setattr(mprepl, "put_file", slow_put)
        th = threading.Thread(
            target=lambda: s.serial_put("loop", "src.txt", "dst.txt"), daemon=True
        )
        th.start()
        assert started.wait(timeout=5), "the transfer never started"
        with pytest.raises(RuntimeError, match="transfer"):
            s.serial_get("loop", "main.py", "dump.py")
        ks = {e["kind"] for e in read_events(tmp_path / "j.jsonl")}
        assert "transfer_refused" in ks, "a get during a transfer passed unjournaled"
        release.set()
        th.join(timeout=10)
    finally:
        s.close()


def test_direct_io_refused_during_transfer_is_journaled(tmp_path, monkeypatch):
    """IH-110: a write interleaved into a raw-REPL transfer corrupts the staged
    file on the board while the put still reports success; a direct read steals
    the exchange's answer bytes. serial_write/serial_read/serial_read_line must
    refuse (and journal) while a transfer is in flight, like reader_start and
    the second transfer already do."""
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("loop", "loop://")
        (tmp_path / "sb" / "src.txt").write_bytes(b"x")
        release = threading.Event()
        started = threading.Event()

        def slow_put(t, data, target_path):
            started.set()
            release.wait(timeout=10)
            return len(data)

        from io_core import mprepl

        monkeypatch.setattr(mprepl, "put_file", slow_put)
        th = threading.Thread(
            target=lambda: s.serial_put("loop", "src.txt", "dst.txt"), daemon=True
        )
        th.start()
        assert started.wait(timeout=5), "the transfer never started"
        with pytest.raises(RuntimeError, match="transfer"):
            s.serial_write("loop", "68656c6c6f")
        with pytest.raises(RuntimeError, match="transfer"):
            s.serial_read("loop")
        with pytest.raises(RuntimeError, match="transfer"):
            s.serial_read_line("loop")
        ks = {e["kind"] for e in read_events(tmp_path / "j.jsonl")}
        for kind in ("write_refused", "read_refused", "read_line_refused"):
            assert kind in ks, f"the {kind} refusal passed unjournaled"
        release.set()
        th.join(timeout=10)
        # the gate must dissolve with the finished transfer, not wedge the name:
        # a normal write goes through right after the exchange ends
        assert s.serial_write("loop", "68656c6c6f") == 5
    finally:
        s.close()


def test_mprepl_stream_buffer_is_bounded():
    """IH-52: _Stream._buf accumulated until the terminator or the deadline -
    a garbage flood (no '>') grew it unboundedly. Past the cap the stream
    raises TransportIoError (journaled by the session caller)."""

    class Flood:
        def read(self, n: int) -> bytes:
            return b"B" * 4096

    st = _Stream(Flood())
    with pytest.raises(TransportIoError, match="exceeded"):
        st.until(b">", time.monotonic() + 10)


def test_expect_read_buffer_is_bounded():
    """IH-52: expect_read accumulated until the needle or the timeout - a
    mismatched flood grew the buffer unboundedly."""

    class Flood:
        def read(self, n: int) -> bytes:
            return b"Z" * 4096

    with pytest.raises(VerificationError, match="exceeded"):
        expect_read(Flood(), b"needle", timeout=30)
