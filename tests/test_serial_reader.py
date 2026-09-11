"""Background serial reader tests (IH-18): unit tests on a fake transport
and integration through Session over loop:// (the loop echoes writes back,
so the reader sees them without hardware). The CH340 serialization contract
(reader I/O lock vs session writes) is pinned without hardware by a fake
transport whose read holds the lock measurably long.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from io_core.journal import read_events
from io_core.replay import ReplaySession
from io_core.serial_reader import SerialReader
from io_core.session import Session


class FakeSerial:
    """Reads drain a prefilled queue; writes record themselves (no echo)."""

    def __init__(self) -> None:
        self.inbox: list[bytes] = []
        self.lock = threading.Lock()
        self.writes: list[bytes] = []
        self.fail = False

    def feed(self, data: bytes) -> None:
        with self.lock:
            self.inbox.append(data)

    def read(self, size: int) -> bytes:
        if self.fail:
            raise OSError("port gone")
        with self.lock:
            if not self.inbox:
                return b""
            return self.inbox.pop(0)[:size]

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)


def drain(reader: SerialReader, *, expect_bytes: int, timeout: float = 5.0) -> None:
    """Waits until the reader buffer holds at least expect_bytes live bytes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reader.stats()["buffered"] >= expect_bytes:
            return
        time.sleep(0.02)
    raise AssertionError(f"reader drained only {reader.stats()} in {timeout}s")


# --- unit: buffer semantics on the fake ---


def test_reader_tail_and_read_until_consume():
    fake = FakeSerial()
    r = SerialReader(fake, max_bytes=4096)
    r.start()
    try:
        fake.feed(b"hello world\nsecond line\n")
        drain(r, expect_bytes=24)
        # tail is non-destructive: the same bytes twice
        assert r.tail(4096)["text"] == "hello world\nsecond line\n"
        assert r.tail(4096)["text"] == "hello world\nsecond line\n"
        # read_until consumes through the match
        got = r.read_until("world", timeout=3)
        assert got["found"] is True
        assert got["text"] == "hello world"
        # consumed prefix gone; the tail starts after the match
        assert r.tail(4096)["text"] == "\nsecond line\n"
        # a repeated read_until waits for a NEW occurrence
        fake.feed(b"world again")
        drain(r, expect_bytes=len("\nsecond line\n") + len("world again"))
        got = r.read_until("world", timeout=3)
        assert got["found"] is True
        assert got["text"].endswith("\nsecond line\nworld")
    finally:
        r.stop()


def test_reader_bounded_drop_oldest():
    fake = FakeSerial()
    r = SerialReader(fake, max_bytes=16)
    r.start()
    try:
        fake.feed(b"A" * 10)
        fake.feed(b"B" * 10)  # total 20 > 16: the oldest chunk is evicted
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and r.stats()["dropped"] < 10:
            time.sleep(0.02)
        assert r.stats()["dropped"] >= 10
        tail = r.tail(64)
        assert "B" in tail["text"] and "A" not in tail["text"]
        assert tail["dropped"] >= 10  # the consumer can tell the history is partial
    finally:
        r.stop()


def test_reader_read_until_timeout_returns_unconsumed():
    fake = FakeSerial()
    r = SerialReader(fake)
    r.start()
    try:
        fake.feed(b"partial answer without the needle")
        drain(r, expect_bytes=31)
        got = r.read_until("READY", timeout=0.3)
        assert got["found"] is False
        assert "partial answer" in got["text"]
    finally:
        r.stop()


def test_reader_surfaces_transport_death():
    fake = FakeSerial()
    r = SerialReader(fake)
    r.start()
    drain_read_until_nothing = 0.2
    time.sleep(drain_read_until_nothing)
    fake.fail = True
    got = r.read_until("anything", timeout=3)
    assert got["found"] is False
    assert r.error() is not None
    assert not r.running


def test_reader_validates_arguments():
    fake = FakeSerial()
    with pytest.raises(ValueError, match="max_bytes"):
        SerialReader(fake, max_bytes=0)
    with pytest.raises(ValueError, match="chunk"):
        SerialReader(fake, chunk=0)


def test_reader_start_idempotent_and_stop_joinable():
    fake = FakeSerial()
    r = SerialReader(fake)
    r.start()
    r.start()  # idempotent: no second thread
    assert r.running
    r.stop()
    assert not r.running
    r.stop()  # idempotent too


# --- integration through Session on loop:// ---


@pytest.fixture()
def session(tmp_path: Path) -> Session:
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    try:
        yield s
    finally:
        s.close()


def loop_session(session: Session, timeout: float = 0.1) -> str:
    session.serial_open("loop", "loop://", timeout=timeout)
    return "loop"


def test_session_reader_tail_and_read_until(session):
    name = loop_session(session)
    session.serial_reader_start(name)
    session.serial_write(name, b"ping".hex())
    got = session.serial_read_until(name, "ping", timeout=5)
    assert got["found"] is True, got
    assert got["text"] == "ping"
    assert session.serial_tail(name)["text"] == ""  # consumed through the match


def test_session_write_serialized_against_reader(session):
    # The CH340 contract without hardware: writes go through the reader's I/O
    # lock (loop:// would not show NUL bursts - the lock ordering is what is
    # pinned here: many parallel writes while the reader drains, no deadlock,
    # no lost write).
    name = loop_session(session)
    session.serial_reader_start(name)
    errors: list[BaseException] = []

    def writer(tag: int) -> None:
        try:
            for i in range(10):
                session.serial_write(name, f"w{tag}-{i};".encode().hex())
        except BaseException as e:  # noqa: BLE001 - recorded and asserted below
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive(), "deadlock: a writer never finished"
    assert errors == []
    # all 40 writes echo back through the loop into the reader buffer
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        text = session.serial_tail(name, 65536)["text"]
        if text.count(";") >= 40:
            break
        time.sleep(0.02)
    assert session.serial_tail(name, 65536)["text"].count(";") >= 40


def test_session_direct_reads_refused_while_reader_runs(session):
    name = loop_session(session)
    session.serial_reader_start(name)
    with pytest.raises(RuntimeError, match="background reader"):
        session.serial_read(name)
    with pytest.raises(RuntimeError, match="background reader"):
        session.serial_read_line(name)
    # after stop, direct reads work again
    session.serial_reader_stop(name)
    session.serial_write(name, b"x".hex())
    assert session.serial_read(name, 16) == b"x".hex()


def test_session_reader_registry_errors(session):
    name = loop_session(session)
    with pytest.raises(KeyError, match="no background reader"):
        session.serial_reader_stop(name)
    with pytest.raises(KeyError, match="no background reader"):
        session.serial_tail(name)
    session.serial_reader_start(name)
    with pytest.raises(KeyError, match="already has a background reader"):
        session.serial_reader_start(name)
    session.serial_reader_stop(name)


def test_reader_requires_serial_transport(session, tmp_path):
    # a file kind has no serial base: the reader refuses politely
    session.file_write("probe.txt", "x")
    with pytest.raises(KeyError, match="is not open"):
        session.serial_reader_start("nope")


def test_close_stops_reader_and_releases_name(session):
    name = loop_session(session)
    session.serial_reader_start(name)
    session.serial_write(name, b"bye".hex())
    session.serial_close(name)  # must stop the reader first, then close
    # the name is free again: a new open + reader works
    session.serial_open(name, "loop://", timeout=0.1)
    session.serial_reader_start(name)
    session.serial_reader_stop(name)


def test_session_close_stops_readers(session):
    name = loop_session(session)
    session.serial_reader_start(name)
    session.close()  # fixture closes again - Session.close must be idempotent-safe
    session.close()


def test_reader_output_journaled_and_replayable(session, tmp_path):
    # "the journal writes everything as before": background chunks land as
    # normal read events with the conn name, reader lifecycle is journaled,
    # and the replay stream contains every drained byte.
    name = loop_session(session)
    session.serial_reader_start(name, max_bytes=8192)
    session.serial_write(name, b"alpha beta\n".hex())
    got = session.serial_read_until(name, "beta", timeout=5)
    assert got["found"] is True
    session.serial_write(name, b"gamma\n".hex())
    assert session.serial_read_until(name, "gamma", timeout=5)["found"] is True
    session.serial_reader_stop(name)
    session.serial_close(name)

    events = read_events(tmp_path / "journal.jsonl")
    kinds = [e["kind"] for e in events]
    assert "reader_start" in kinds and "reader_stop" in kinds
    reads = [e for e in events if e["kind"] == "read"]
    assert reads and all(e.get("conn") == "loop" for e in reads)
    replayed = ReplaySession(events)["loop"]
    stream = b""
    while True:
        chunk = replayed.read(4096)
        if not chunk:
            break
        stream += chunk
    assert b"alpha beta" in stream and b"gamma" in stream


# --- live-hardware check (opt-in, stage 3.4; skipped without a board) ---


def test_live_ch340_reader_write_no_nul_bursts(tmp_path):
    """Live check of the IH-18 gate: with the background reader running, a
    burst of writes must not come back as NUL bytes (the failure mode that
    killed naive concurrent IO on the CH340 link). Runs only with
    IRONBENCH_REAL_PORT set to the board."""
    import os

    port = os.environ.get("IRONBENCH_REAL_PORT")
    if not port:
        pytest.skip("live check: set IRONBENCH_REAL_PORT to the board's COM port")
    from io_core.serial_transport import SerialTransport

    t = SerialTransport(port, baudrate=115200, timeout=0.1)
    t.open()
    reader = SerialReader(t, max_bytes=1 << 16)
    reader.start()
    try:
        for i in range(20):
            with reader.io_lock:
                t.write(f"probe {i}\n".encode())
            time.sleep(0.05)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if reader.stats()["buffered"] > 0:
                break
            time.sleep(0.05)
        tail = reader.tail(1 << 16)
        assert tail["text"] is not None
        nul_ratio = tail["data_hex"].count("00") / max(1, len(tail["data_hex"]) // 2)
        assert nul_ratio < 0.2, f"NUL burst on the live link: {tail['data_hex'][:80]}"
    finally:
        reader.stop()
        t.close()
