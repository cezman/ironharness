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
from typing import Self

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
    r = SerialReader(fake, max_bytes=16, chunk=16)
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
    # max_bytes < chunk: every chunk would be evicted whole - refuse loudly
    with pytest.raises(ValueError, match=">= chunk"):
        SerialReader(fake, max_bytes=8, chunk=16)


def test_read_until_rejects_empty_pattern():
    fake = FakeSerial()
    r = SerialReader(fake)
    r.start()
    try:
        with pytest.raises(ValueError, match="pattern"):
            r.read_until("")
    finally:
        r.stop()


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
    # pinned here: parallel writes while the reader drains, no deadlock, no
    # lost write). Deliberately small load and generous joins: free CI
    # runners are slow and shared; a wall-clock-heavy variant of this test
    # flaked there (writers "never finished" within 30 s while the suite
    # around stayed green). The serialization itself is pinned exactly by
    # test_write_takes_the_reader_io_lock and the lock-order test below.
    import faulthandler

    name = loop_session(session)
    session.serial_reader_start(name)
    errors: list[BaseException] = []

    def writer(tag: int) -> None:
        try:
            for i in range(2):
                session.serial_write(name, f"w{tag}-{i};".encode().hex())
        except BaseException as e:  # noqa: BLE001 - recorded and asserted below
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,), daemon=True) for t in range(2)]
    for t in threads:
        t.start()
    # if a writer wedges, dump every thread's stack right in the log - the
    # next incident must show WHERE it stands, not just that it timed out
    faulthandler.dump_traceback_later(45, exit=False)
    try:
        for t in threads:
            t.join(timeout=60)
            assert not t.is_alive(), "deadlock: a writer never finished"
    finally:
        faulthandler.cancel_dump_traceback_later()
    assert errors == []
    # all 4 writes echo back through the loop into the reader buffer
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        text = session.serial_tail(name, 65536)["text"]
        if text.count(";") >= 4:
            break
        time.sleep(0.02)
    assert session.serial_tail(name, 65536)["text"].count(";") >= 4


class TrackingLock:
    """Records every acquisition with the acquirer's thread id
    (list.append is atomic under the GIL)."""

    def __init__(self, inner: threading.Lock) -> None:
        self._inner = inner
        self.entered: list[tuple[int, float]] = []

    def __enter__(self) -> Self:
        self._inner.__enter__()
        self.entered.append((threading.get_ident(), time.monotonic()))
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.__exit__(*exc)

    def entered_by(self, thread_id: int) -> int:
        return sum(1 for tid, _ in self.entered if tid == thread_id)


def test_write_takes_the_reader_io_lock(session):
    # The M1 pin: without `with reader.io_lock` in Session.serial_write the
    # whole offline suite stays green - the CH340 serialization would be a
    # promise without a mechanism. Instrumented lock: a session write while a
    # reader runs MUST pass through the reader's I/O lock.
    name = loop_session(session)
    session.serial_reader_start(name)
    reader = session._readers[name]
    tracking = TrackingLock(threading.Lock())
    reader._io_lock = tracking
    before = len(tracking.entered)
    session.serial_write(name, b"x".hex())
    assert len(tracking.entered) > before, "serial_write bypassed the reader I/O lock"
    session.serial_reader_stop(name)


def test_write_lock_order_session_lock_before_io_lock(session):
    # Deterministic lock-order pin (the probabilistic stop-race test cannot
    # catch the regression): while THIS thread holds the session lock, a
    # concurrent serial_write (reader attached) must NOT touch the reader's
    # I/O lock. The round-1 deadlock form took io_lock FIRST and then waited
    # for the session lock inside _get - here the writer's io-lock entry
    # would appear while the session lock is still held.
    name = loop_session(session)
    session.serial_reader_start(name)
    reader = session._readers[name]
    probe = TrackingLock(threading.Lock())
    reader._io_lock = probe
    writer = threading.Thread(target=session.serial_write, args=(name, b"z".hex()), daemon=True)
    import faulthandler

    with session._lock:  # the writer below can only block on THIS lock
        writer.start()
        time.sleep(0.3)  # writer is parked on the session lock acquisition
        assert probe.entered_by(writer.ident) == 0, (
            "serial_write took the reader I/O lock before the session lock "
            "(the deadlock half-cycle)"
        )
    faulthandler.dump_traceback_later(10, exit=False)
    try:
        writer.join(timeout=15)
    finally:
        faulthandler.cancel_dump_traceback_later()
    assert not writer.is_alive()
    assert probe.entered_by(writer.ident) == 1  # after the release, the write ran under the lock
    session.serial_reader_stop(name)


def test_reader_stop_does_not_deadlock_against_inflight_write(session):
    # Review round-1 blocker: serial_write used to hold the reader I/O lock
    # while waiting for the session lock (_get inside the lock block), and
    # serial_reader_stop held the session lock while joining the reader
    # thread, which waited for the I/O lock - a cycle that froze the whole
    # session for the join timeout. Now the session lock is always released
    # before the I/O lock is taken; this test reproduces the interleaving
    # (a write in flight while stop joins) and pins a fast stop.
    name = loop_session(session)
    session.serial_reader_start(name)
    stop_done = threading.Event()
    errors: list[BaseException] = []

    def stopper() -> None:
        try:
            session.serial_reader_stop(name)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        stop_done.set()

    started = threading.Event()

    def inflight_writer() -> None:
        started.set()
        try:
            for _ in range(20):
                session.serial_write(name, b"y".hex())
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    writer_thread = threading.Thread(target=inflight_writer, daemon=True)
    writer_thread.start()
    started.wait(timeout=5)
    t0 = time.monotonic()
    threading.Thread(target=stopper, daemon=True).start()
    assert stop_done.wait(timeout=15), "reader_stop deadlocked against an in-flight write"
    # a real lock cycle costs the full join timeout inside reader.stop() (5 s
    # + epsilon); a healthy stop returns after the current read (~0.1 s), so
    # anything near 5 s IS the cycle, even on a slow runner
    assert time.monotonic() - t0 < 4.8, "reader_stop blocked for the join timeout (lock cycle)"
    writer_thread.join(timeout=60)
    assert not writer_thread.is_alive()
    assert errors == []


def reader_threads_alive() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "serial-reader")


def implicit_reader_stops(journal_path: Path) -> list[dict]:
    return [
        e
        for e in read_events(journal_path)
        if e["kind"] == "reader_stop" and e.get("implicit")
    ]


def test_close_transport_stops_and_journals_reader(session, tmp_path):
    name = loop_session(session)
    session.serial_reader_start(name)
    assert reader_threads_alive() >= 1
    session.close_transport(name)
    # the journal pin (the thread-count assert alone is decorative on loop://:
    # a closed port kills the reader thread by itself): closing the transport
    # must END the reader in the journal, not just in memory
    stops = implicit_reader_stops(tmp_path / "journal.jsonl")
    assert stops and stops[-1].get("conn") == "loop"
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and reader_threads_alive():
        time.sleep(0.05)
    assert reader_threads_alive() == 0


def test_session_close_stops_and_journals_reader(session, tmp_path):
    name = loop_session(session)
    session.serial_reader_start(name)
    assert reader_threads_alive() >= 1
    session.close()  # stops every reader (journaling each) before transports
    stops = implicit_reader_stops(tmp_path / "journal.jsonl")
    assert stops and stops[-1].get("conn") == "loop"
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and reader_threads_alive():
        time.sleep(0.05)
    assert reader_threads_alive() == 0
    session.close()  # idempotent


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


def test_reader_output_journaled_and_replayable(session, tmp_path):
    # "the journal writes everything as before": background chunks land as
    # normal read events with the conn name, reader lifecycle is journaled
    # (explicit AND implicit stops - a reader must end in the journal, not
    # vanish), and the replay stream contains every drained byte.
    name = loop_session(session)
    session.serial_reader_start(name, max_bytes=8192)
    session.serial_write(name, b"alpha beta\n".hex())
    got = session.serial_read_until(name, "beta", timeout=5)
    assert got["found"] is True
    session.serial_write(name, b"gamma\n".hex())
    assert session.serial_read_until(name, "gamma", timeout=5)["found"] is True
    session.serial_reader_stop(name)
    # a second reader, ended implicitly by the transport close
    session.serial_reader_start(name)
    session.serial_close(name)

    events = read_events(tmp_path / "journal.jsonl")
    kinds = [e["kind"] for e in events]
    assert kinds.count("reader_start") == 2 and kinds.count("reader_stop") == 2
    implicit = [e for e in events if e["kind"] == "reader_stop" and e.get("implicit")]
    assert implicit and implicit[0].get("conn") == "loop"
    # reader_start precedes any background read of that reader (journal order
    # must not lie about causality)
    assert kinds.index("reader_start") < next(i for i, k in enumerate(kinds) if k == "read")
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
