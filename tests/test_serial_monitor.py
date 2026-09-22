"""IH-83 serial_monitor: a bounded foreground read window on a serial
transport - max_bytes/max_seconds caps, quiet window, stop pattern, sandbox
dump, journal. Hostile cases are mandatory (checklist 7a): an endless flood,
one giant newline-less blob, and a silent port must all stop inside the
caps, and every refusal must land in the journal.
"""

from __future__ import annotations

import threading
import time

import pytest

from io_core import mprepl
from io_core.errors import TransportClosedError
from io_core.journal import read_events
from io_core.session import Session


def _open_session(tmp_path):
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    s.serial_open("c", "loop://", timeout=0.1)
    return s


def _port(s: Session):
    """The raw loop:// handle - the test feeds bytes through it, the monitor
    drains them through the session wrappers."""
    return s._serial_base["c"]


def _kinds(tmp_path) -> set[str]:
    return {e["kind"] for e in read_events(tmp_path / "j.jsonl")}


def _events(tmp_path, kind: str) -> list[dict]:
    return [e for e in read_events(tmp_path / "j.jsonl") if e["kind"] == kind]


# --- stop conditions ---


def test_stops_on_bytes_cap_with_giant_line(tmp_path):
    """Hostile 7a: a huge newline-less stream. The monitor is byte-capped,
    not line-oriented - the cap must hold, not hang on a missing \\n. The
    feed runs while the monitor drains (loop:// buffers are bounded), the
    stream itself never sees a newline."""
    s = _open_session(tmp_path)
    try:
        fed = threading.Event()

        def feed():
            for _ in range(8):
                try:
                    _port(s).write(b"A" * 4096)
                except OSError:  # port closed under us - done
                    return
                time.sleep(0.02)
            fed.set()

        th = threading.Thread(target=feed, daemon=True)
        th.start()
        try:
            t0 = time.monotonic()
            r = s.serial_monitor("c", max_bytes=1024, max_seconds=30)
            elapsed = time.monotonic() - t0
        finally:
            th.join(timeout=10)
        assert r["stop_reason"] == "max_bytes"
        assert r["truncated"] is True
        assert r["bytes"] == 1024
        assert set(r["text"]) == {"A"}
        assert elapsed < 15, "the byte cap must not wait out the deadline"
    finally:
        s.close()


def test_stops_on_pattern(tmp_path):
    s = _open_session(tmp_path)
    try:
        _port(s).write(b"noise line\nBOOT OK\nmore")
        t0 = time.monotonic()
        r = s.serial_monitor("c", max_seconds=30, stop_pattern="BOOT OK")
        elapsed = time.monotonic() - t0
        assert r["stop_reason"] == "pattern"
        assert "BOOT OK" in r["text"]
        assert r["truncated"] is False
        assert elapsed < 15
    finally:
        s.close()


def test_stops_on_quiet_window(tmp_path):
    s = _open_session(tmp_path)
    try:
        _port(s).write(b"hello\n")
        t0 = time.monotonic()
        r = s.serial_monitor("c", max_seconds=30, quiet_seconds=0.3)
        elapsed = time.monotonic() - t0
        assert r["stop_reason"] == "quiet"
        assert "hello" in r["text"]
        assert elapsed < 15, "a quiet window must not wait out the deadline"
    finally:
        s.close()


def test_stops_on_seconds_cap_when_silent(tmp_path):
    s = _open_session(tmp_path)
    try:
        t0 = time.monotonic()
        r = s.serial_monitor("c", max_seconds=0.6)
        elapsed = time.monotonic() - t0
        assert r["stop_reason"] == "max_seconds"
        assert r["bytes"] == 0
        assert 0.5 <= elapsed < 5
    finally:
        s.close()


def test_seconds_cap_beats_endless_flood(tmp_path):
    """Hostile 7a: a device that never stops emitting. Without a cap the
    monitor would never return; the deadline must end it."""
    s = _open_session(tmp_path)
    try:
        port = _port(s)
        stop = threading.Event()

        def flood():
            payload = b"x" * 256
            while not stop.is_set():
                try:
                    port.write(payload)
                except OSError:  # port closed under us - done
                    return
                time.sleep(0.001)

        th = threading.Thread(target=flood, daemon=True)
        th.start()
        try:
            t0 = time.monotonic()
            r = s.serial_monitor("c", max_seconds=0.7, max_bytes=1_048_576)
            elapsed = time.monotonic() - t0
        finally:
            stop.set()
            th.join(timeout=10)
        assert r["stop_reason"] == "max_seconds"
        assert r["bytes"] > 0
        assert elapsed < 5, "the deadline must cut an endless flood short"
    finally:
        s.close()


# --- argument bounds (IH-78 class): refusals are journaled ---


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"max_bytes": 2_000_000},
        {"max_seconds": 0},
        {"max_seconds": 3601},
        {"quiet_seconds": 0},
        {"quiet_seconds": -1},
        {"stop_pattern": ""},  # IH-116 class: "" matches instantly, a lie
        {"dump_path": ""},  # IH-116 class: "" silently means "no dump"
    ],
)
def test_argument_bounds_refused_and_journaled(tmp_path, kwargs):
    s = _open_session(tmp_path)
    try:
        with pytest.raises(ValueError):
            s.serial_monitor("c", **kwargs)
    finally:
        s.close()
    refusals = _events(tmp_path, "monitor_refused")
    assert len(refusals) == 1
    assert "reason" in refusals[0]


# --- the monitor owns the read side: the gate network must hold ---


def test_gates_refuse_while_monitor_runs(tmp_path):
    s = _open_session(tmp_path)
    try:
        (tmp_path / "sb" / "src.txt").write_bytes(b"x")
        done = threading.Event()

        def run():
            s.serial_monitor("c", max_seconds=1.5)
            done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        time.sleep(0.2)  # the monitor is inside its read window now
        with pytest.raises(RuntimeError, match="monitor"):
            s.serial_write("c", "68656c6c6f")
        with pytest.raises(RuntimeError, match="monitor"):
            s.serial_read("c")
        with pytest.raises(RuntimeError, match="monitor"):
            s.serial_read_line("c")
        with pytest.raises(RuntimeError, match="monitor"):
            s.serial_put("c", "src.txt", "dst.txt")
        with pytest.raises(RuntimeError, match="monitor"):
            s.serial_get("c", "dst.txt", "back.txt")
        with pytest.raises(RuntimeError, match="monitor"):
            s.serial_reader_start("c")
        with pytest.raises(RuntimeError, match="monitor"):
            s.serial_monitor("c", max_seconds=0.1)
        assert s.status()["monitors"] == ["c"]
        assert done.wait(timeout=10)
        assert s.status()["monitors"] == []
        s.serial_write("c", "6f6b")  # the gates lift after the monitor ends
        th.join(timeout=10)
    finally:
        s.close()
    ks = _kinds(tmp_path)
    for kind in (
        "write_refused",
        "read_refused",
        "read_line_refused",
        "put_refused",
        "get_refused",
        "reader_start_refused",
        "monitor_refused",
    ):
        assert kind in ks, f"{kind} passed unjournaled"


def test_monitor_refused_while_reader_attached(tmp_path):
    s = _open_session(tmp_path)
    try:
        s.serial_reader_start("c")
        with pytest.raises(RuntimeError, match="reader"):
            s.serial_monitor("c", max_seconds=0.2)
    finally:
        s.close()
    refusals = _events(tmp_path, "monitor_refused")
    assert len(refusals) == 1
    assert "reader" in refusals[0]["reason"]


def test_monitor_refused_while_transfer_in_flight(tmp_path, monkeypatch):
    s = _open_session(tmp_path)
    try:
        (tmp_path / "sb" / "src.txt").write_bytes(b"x")
        release = threading.Event()
        started = threading.Event()

        def slow_put(t, data, target_path):
            started.set()
            release.wait(timeout=10)
            return len(data)

        monkeypatch.setattr(mprepl, "put_file", slow_put)
        th = threading.Thread(
            target=lambda: s.serial_put("c", "src.txt", "dst.txt"), daemon=True
        )
        th.start()
        assert started.wait(timeout=5), "the transfer never started"
        with pytest.raises(RuntimeError, match="transfer"):
            s.serial_monitor("c", max_seconds=0.2)
        release.set()
        th.join(timeout=10)
    finally:
        s.close()
    refusals = _events(tmp_path, "monitor_refused")
    assert len(refusals) == 1
    assert "transfer" in refusals[0]["reason"]


def test_unknown_conn_journals_failure(tmp_path):
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        with pytest.raises(KeyError):
            s.serial_monitor("nope", max_seconds=0.1)
    finally:
        s.close()
    failures = _events(tmp_path, "monitor_failed")
    assert len(failures) == 1


def test_serial_close_during_window_ends_monitor_honestly(tmp_path):
    """The docstring promise: a transport closed mid-window ends the monitor
    with the real error - journaled as monitor_failed, the name released
    (a stuck name would refuse every later write on the conn)."""
    s = _open_session(tmp_path)
    try:
        done = threading.Event()

        def run():
            try:
                s.serial_monitor("c", max_seconds=30)
            except (TransportClosedError, OSError):
                pass  # the transport is dead - the monitor surfaced it instead of hanging
            done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        time.sleep(0.3)  # the monitor is inside its read window now
        s.serial_close("c")
        assert done.wait(timeout=10), "closing the transport must end the monitor"
        assert s.status()["monitors"] == []
        failures = _events(tmp_path, "monitor_failed")
        assert len(failures) == 1
        th.join(timeout=10)
    finally:
        s.close()


def test_reset_stays_ungated_during_window(tmp_path):
    """serial_reset is the recovery hatch (the IH-110 precedent): it must
    pass while a monitor runs - boot output after a reset is just more
    capture. A future 'helpful' gate here would kill that escape hatch."""
    s = _open_session(tmp_path)
    try:
        done = threading.Event()

        def run():
            s.serial_monitor("c", max_seconds=10, quiet_seconds=1.0)
            done.set()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        time.sleep(0.3)  # the monitor is inside its read window now
        s.serial_reset("c", pulse_sec=0.05, settle_sec=0.2)  # must not raise
        assert done.wait(timeout=15)
        th.join(timeout=10)
    finally:
        s.close()


# --- dump, journal, reuse ---


def test_dump_to_sandbox_and_reuse_overwrites(tmp_path):
    s = _open_session(tmp_path)
    try:
        _port(s).write(b"FIRST-PAYLOAD")
        r1 = s.serial_monitor(
            "c", max_seconds=10, quiet_seconds=0.3, dump_path="cap.bin"
        )
        assert r1["dump"] == "cap.bin"
        assert "dump_error" not in r1
        first = (tmp_path / "sb" / "cap.bin").read_bytes()
        assert first == r1["text"].encode("utf-8", "replace")

        _port(s).write(b"SECOND")
        r2 = s.serial_monitor(
            "c", max_seconds=10, quiet_seconds=0.3, dump_path="cap.bin"
        )
        assert r2["dump"] == "cap.bin"
        second = (tmp_path / "sb" / "cap.bin").read_bytes()
        assert second != first, "a rerun into the same dump must overwrite"
        assert second == r2["text"].encode("utf-8", "replace")
    finally:
        s.close()


def test_dump_outside_sandbox_fails_soft(tmp_path):
    """A failed dump must not eat the capture: the summary still returns and
    the failure is journaled - but no dump path may be claimed."""
    s = _open_session(tmp_path)
    try:
        _port(s).write(b"DATA")
        r = s.serial_monitor(
            "c", max_seconds=10, quiet_seconds=0.3, dump_path="../evil.bin"
        )
        assert "dump" not in r
        assert "dump_error" in r
    finally:
        s.close()
    assert "monitor_dump_failed" in _kinds(tmp_path)


def test_summary_is_journaled_with_conn_attribution(tmp_path):
    s = _open_session(tmp_path)
    try:
        _port(s).write(b"hello\n")
        s.serial_monitor("c", max_seconds=10, quiet_seconds=0.3)
    finally:
        s.close()
    events = _events(tmp_path, "serial_monitor")
    assert len(events) == 1
    e = events[0]
    assert e["conn"] == "c"
    assert e["bytes"] > 0
    assert e["stop_reason"] == "quiet"
    # the summary event carries only stats: the per-chunk reads keep the
    # data_hex audit trail, the dump is the agent-facing copy
    assert "data" not in e and "data_hex" not in e
    # the drained chunks are journaled as reads with the conn name, like any read
    read_events_c = [x for x in read_events(tmp_path / "j.jsonl") if x["kind"] == "read"]
    assert read_events_c, "monitor chunk reads must be journaled"
    assert all(x.get("conn") == "c" for x in read_events_c)
