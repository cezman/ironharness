"""IH-32 io-core robustness tests: hostile/broken inputs must produce honest
typed errors, denials must be journaled, and the journal's multi-writer lock
must hold across processes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from io_core.errors import JournalCorrupt, QuotaExceeded, SandboxViolation
from io_core.file_sandbox import FileSandbox
from io_core.journal import JsonlJournal, read_events, read_events_chain
from io_core.replay import ReplayMismatch, ReplayTransport

# --- journal: payload cannot shadow the service keys ---


def test_payload_cannot_shadow_service_keys(tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="t") as j:
        j("my_kind", {"kind": "EVIL", "ts": 0, "actor": "EVIL", "seq": 999, "data": "ok"})
    events = read_events(jpath)
    main = events[0]
    assert main["kind"] == "my_kind"
    assert main["actor"] == "t"
    assert main["seq"] == 1
    assert main["data"] == "ok"
    # the conflict is journaled separately, not silently dropped
    conflict = events[1]
    assert conflict["kind"] == "journal_key_conflict"
    assert conflict["requested_kind"] == "my_kind"
    assert sorted(conflict["keys"]) == ["actor", "kind", "seq", "ts"]


def test_session_hook_conn_cannot_be_shadowed(tmp_path):
    from io_core.session import Session

    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("c", "loop://", timeout=0.1)
        s.serial_write("c", b"hi".hex())
    finally:
        s.close()
    for e in read_events(tmp_path / "j.jsonl"):
        if "conn" in e:
            assert e["conn"] == "c"


# --- journal: multi-writer file lock across PROCESSES ---


WRITER_SCRIPT = """
import sys
from io_core.journal import JsonlJournal
path, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
with JsonlJournal(path, actor=tag) as j:
    for i in range(n):
        j("event", {"tag": tag, "n": i})
"""


def test_two_processes_do_not_lose_lines(tmp_path):
    # The pre-v0.7.0 audit: two processes appending to one file lost 5-12 of
    # 1000 lines on Windows (O_APPEND across handles is not atomic). The
    # file-region lock around write+flush makes the loss impossible.
    import os

    jpath = tmp_path / "shared.jsonl"
    n = 300
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER_SCRIPT, str(jpath), tag, str(n)],
            cwd=str(Path(__file__).parents[1]),
            env=env,
        )
        for tag in ("a", "b")
    ]
    for p in procs:
        assert p.wait(timeout=120) == 0
    events = read_events(jpath)
    assert len(events) == 2 * n, f"lost lines: {2 * n - len(events)}"
    for tag in ("a", "b"):
        got = [e for e in events if e["actor"] == tag]
        assert [e["n"] for e in got] == list(range(n))  # per-writer order kept


WRITER_SCRIPT_ROTATION = """
import sys
import time
from io_core.journal import JsonlJournal
path, tag, n, max_bytes = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
# max_files high enough to retain every part: the test asserts no line is
# lost, so rotation must never drop a part that still holds events
with JsonlJournal(path, actor=tag, max_bytes=max_bytes, max_files=40) as j:
    # overlap the writers: subprocess start jitter (~1 s of imports) usually
    # exceeds the whole write window, and non-overlapping writers never hit
    # the rotation-vs-open-handle collision this test exists for
    time.sleep(0.5)
    for i in range(n):
        j("event", {"tag": tag, "n": i})
"""


def test_two_processes_survive_rotation(tmp_path):
    # IH-72: rotation must be compatible with a second writer holding the
    # journal open. With a persistent per-instance handle the replace() during
    # rotation failed on Windows (PermissionError, WinError 32) while the
    # other process had the file open; on POSIX the other writer's handle
    # silently followed the rename and its events landed in the archived part.
    import os

    jpath = tmp_path / "shared.jsonl"
    n, max_bytes = 80, 700
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER_SCRIPT_ROTATION, str(jpath), tag, str(n), str(max_bytes)],
            cwd=str(Path(__file__).parents[1]),
            env=env,
        )
        for tag in ("a", "b")
    ]
    for p in procs:
        assert p.wait(timeout=120) == 0, f"writer {p.args[4]} crashed"
    events = read_events_chain(jpath)
    assert len(events) == 2 * n, f"lost lines: {2 * n - len(events)}"
    for tag in ("a", "b"):
        got = [e for e in events if e.get("actor") == tag]
        assert [e["n"] for e in got] == list(range(n))  # per-writer order kept


def test_closed_journal_rejects_writes(tmp_path):
    # IH-72 kept the close contract: a closed journal must not accept writes
    j = JsonlJournal(tmp_path / "j.jsonl", actor="t")
    j("event", {"n": 1})
    j.close()
    with pytest.raises(ValueError):
        j("event", {"n": 2})


def test_replay_reads_rotated_parts(tmp_path):
    # IH-75 (review): replay read only the live journal file - a session
    # whose journal had rotated lost its early history (strict replay then
    # failed on writes it could not see). The rotation chain must be read.
    import json

    from io_core.replay import ReplaySession, ReplayTransport

    def ev(seq, kind, hex_):
        return json.dumps(
            {
                "ts": seq,
                "seq": seq,
                "actor": "t",
                "conn": "dev",
                "kind": kind,
                "data_hex": hex_,
            }
        ) + "\n"

    (tmp_path / "j.jsonl.1").write_text(ev(1, "write", "cafe"), encoding="utf-8")
    (tmp_path / "j.jsonl").write_text(ev(2, "read", "beef"), encoding="utf-8")

    transport = ReplayTransport.from_file(tmp_path / "j.jsonl", conn="dev")
    transport.write(b"\xca\xfe")  # recorded in the rotated part
    transport.close()  # strict: every recorded write must be replayed

    session = ReplaySession.from_file(tmp_path / "j.jsonl")
    session_dev = session["dev"]
    session_dev.write(b"\xca\xfe")
    session_dev.close()


def test_replay_missing_journal_fails_loudly(tmp_path):
    # IH-75 review: the chain read must not turn a missing journal into a
    # vacuously green empty replay - a typo stays a loud error
    from io_core.replay import ReplaySession, ReplayTransport

    with pytest.raises(FileNotFoundError):
        ReplayTransport.from_file(tmp_path / "nope.jsonl")
    with pytest.raises(FileNotFoundError):
        ReplaySession.from_file(tmp_path / "nope.jsonl")


# --- journal: read_events rejects corrupt lines honestly ---


def test_read_events_corrupt_line_is_journal_corrupt(tmp_path):
    jpath = tmp_path / "j.jsonl"
    jpath.write_text(
        json.dumps({"kind": "ok"}) + "\n" + "not-json{{{" + "\n", encoding="utf-8"
    )
    with pytest.raises(JournalCorrupt, match="not valid JSON"):
        read_events(jpath)


def test_read_events_non_object_line_is_journal_corrupt(tmp_path):
    jpath = tmp_path / "j.jsonl"
    jpath.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(JournalCorrupt, match="not a JSON object"):
        read_events(jpath)


# --- replay: unreadable data_hex and unreplayed writes ---


def test_replay_unreadable_data_hex_is_journal_corrupt():
    with pytest.raises(JournalCorrupt, match="unreadable data_hex"):
        ReplayTransport([{"kind": "read", "conn": "", "data_hex": "zz"}])


def test_replay_strict_close_reports_never_replayed_writes(tmp_path):
    events = [
        {"kind": "write", "conn": "", "data_hex": "aabb"},
        {"kind": "write", "conn": "", "data_hex": "cc"},
    ]
    # partial reproduction: close() reports the missing write
    with pytest.raises(ReplayMismatch, match="1 byte"), ReplayTransport(events) as rp:
        rp.write(b"\xaa\xbb")  # only the first of two writes reproduced
    # zero reproduction: close() reports everything missing
    with pytest.raises(ReplayMismatch, match="3 byte"):
        ReplayTransport(events).close()  # nothing replayed at all
    # full reproduction: close() is silent
    with ReplayTransport(events) as rp:
        rp.write(b"\xaa\xbb")
        rp.write(b"\xcc")


def test_replay_close_is_idempotent():
    rp = ReplayTransport([], strict=True)
    rp.close()
    rp.close()  # no second raise/no error on an empty journal


# --- sandbox: denials are journaled ---


def test_sandbox_escape_and_quota_denials_are_journaled(tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="t") as j:
        sb = FileSandbox(tmp_path / "sb", max_bytes=10, max_files=2, on_event=j)
        with pytest.raises(SandboxViolation):
            sb.resolve("../outside.txt")
        with pytest.raises(QuotaExceeded):
            sb.write_file("big.bin", b"x" * 100)
    kinds = [e["kind"] for e in read_events(jpath)]
    assert "sandbox_violation" in kinds
    assert "quota_denied" in kinds
    quota = next(e for e in read_events(jpath) if e["kind"] == "quota_denied")
    assert quota["limit_kind"] == "bytes" and quota["limit"] == 10


def test_sandbox_quota_is_per_instance_documented(tmp_path):
    # two instances on one root see each other's files, but neither enforces
    # the other's limit: each check is "would MY limit break" (the docstring
    # contract; no combined budget is guaranteed)
    sb1 = FileSandbox(tmp_path / "sb", max_bytes=100)
    sb2 = FileSandbox(tmp_path / "sb", max_bytes=1000)
    sb1.write_file("a.bin", b"x" * 90)
    # sb2 has a LARGER limit: it happily exceeds sb1's budget (its check sees
    # 90+90=180 against its own 1000) - no combined budget exists
    sb2.write_file("b.bin", b"y" * 90)
    assert sb1._usage()[0] == 180  # both files are in the shared tree
    # and each instance still enforces ITS OWN limit on its own writes
    with pytest.raises(QuotaExceeded):
        sb1.write_file("c.bin", b"z" * 90)  # 180+90 > sb1's 100


# --- transports: closed operations raise typed errors (python -O proof) ---


def test_closed_transport_operations_raise_typed_errors():
    from io_core.errors import TransportClosedError
    from io_core.limits import DeadlineTransport
    from io_core.serial_transport import SerialTransport

    t = SerialTransport("loop://", timeout=0.05)
    for op in (lambda: t.write(b"x"), lambda: t.read(1), lambda: t.read_line()):
        with pytest.raises(TransportClosedError, match="port is not open"):
            op()
    d = DeadlineTransport(t, 5.0)
    with pytest.raises(TransportClosedError, match="not open"):
        d._check()


def test_deadline_transport_check_survives_python_O():
    # the point of the assert->error replacement: run a probe under python -O
    code = (
        "from io_core.limits import DeadlineTransport\n"
        "from io_core.errors import TransportClosedError\n"
        "class T:\n"
        "    def open(self): pass\n"
        "    def close(self): pass\n"
        "d = DeadlineTransport(T(), 1.0)\n"
        "try:\n"
        "    d._check()\n"
        "except TransportClosedError:\n"
        "    print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-O", "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parents[1]),
        timeout=30,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_session_operations_after_close_raise(tmp_path):
    from io_core.errors import TransportClosedError
    from io_core.session import Session

    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    s.close()
    with pytest.raises(TransportClosedError, match="session is closed"):
        s.serial_open("x", "loop://")
    with pytest.raises(TransportClosedError, match="session is closed"):
        s.file_write("a.txt", "x")


# --- modbus_sim survives hostile frames ---


def test_modbus_sim_survives_mbap_length_zero_and_truncated_fc16():
    import socket
    import struct

    from io_core.modbus_sim import ModbusSimServer

    srv = ModbusSimServer()
    srv.start()
    try:
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=3)
        # MBAP length=0: promises no PDU - the link is dropped, the
        # server must live
        sock.sendall(struct.pack(">HHHB", 1, 0, 0, 1))
        sock.settimeout(2)
        try:
            sock.recv(64)
        except (ConnectionResetError, OSError):
            pass  # dropped link is the expected outcome
        # the server still serves a fresh client
        sock2 = socket.create_connection(("127.0.0.1", srv.port), timeout=3)
        # MBAP length=1 (valid MBAP, empty PDU): the handler must ANSWER a
        # modbus exception and KEEP SERVING on the same socket - this pins
        # the handler's survival, which a fresh-connection check cannot see
        # (ThreadingTCPServer survives a dead handler thread regardless)
        sock2.sendall(struct.pack(">HHHB", 2, 0, 1, 1))
        resp = sock2.recv(64)
        assert resp[7] == 0x80 and resp[8] == 0x01
        pdu = struct.pack(">BHH", 3, 0, 2)  # FC3 read 2 registers
        sock2.sendall(struct.pack(">HHHB", 3, 0, len(pdu) + 1, 1) + pdu)
        resp = sock2.recv(64)
        assert resp[7] == 3  # FC3 echo on the SAME connection, no exception
        # truncated FC16 (qty promises 4 data bytes, frame carries 2):
        # a modbus exception (0x90 0x03), not a dead thread
        pdu_bad = struct.pack(">BHH", 16, 0, 2) + b"\x00\x01"  # promises 2 regs, carries 1
        sock2.sendall(struct.pack(">HHHB", 4, 0, len(pdu_bad) + 1, 1) + pdu_bad)
        resp = sock2.recv(64)
        assert resp[7] == 0x90 and resp[8] == 0x03
        # and the sim still works after the abuse
        sock2.sendall(struct.pack(">HHHB", 5, 0, len(pdu) + 1, 1) + pdu)
        resp = sock2.recv(64)
        assert resp[7] == 3
        sock2.close()
        sock.close()
    finally:
        srv.stop()


def test_limit_denials_are_journaled(tmp_path, monkeypatch):
    # PLAN fold-in (IH-29 remainder): the limits wrappers had no on_event
    # mechanism, so deadline/rate denials never reached the journal.
    from io_core.session import Session

    monkeypatch.setenv("IRONHARNESS_TRANSPORT_RATE", "2/60")
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("c", "loop://", timeout=0.05)
        s.serial_write("c", b"1".hex())
        s.serial_write("c", b"2".hex())
        with pytest.raises(Exception):  # noqa: B017 - RateLimitExceeded on 3rd
            s.serial_write("c", b"3".hex())
        wrapper = s._transports["c"]
        assert type(wrapper).__name__ == "RateLimitedTransport"
    finally:
        s.close()
    kinds = [e["kind"] for e in read_events(tmp_path / "j.jsonl")]
    assert "rate_denied" in kinds


def test_deadline_denial_is_journaled(tmp_path, monkeypatch):
    from io_core.session import Session

    class SlowTransport:
        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

        def read(self, size: int) -> bytes:
            return b""

    monkeypatch.delenv("IRONHARNESS_TRANSPORT_RATE", raising=False)
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("c", "loop://", timeout=0.05)
        # force an expired deadline on the live wrapper: open()-time is reset
        # to far in the past via the wrapper's clock hook
        wrapper = s._transports["c"]
        assert type(wrapper).__name__ == "DeadlineTransport"
        wrapper._started = time.monotonic() - 10**6
        with pytest.raises(Exception):  # noqa: B017 - OperationTimeout
            s.serial_write("c", b"1".hex())
    finally:
        s.close()
    kinds = [e["kind"] for e in read_events(tmp_path / "j.jsonl")]
    assert "deadline_denied" in kinds


# --- report: typed garbage does not crash the aggregate ---


def test_report_tolerates_typed_garbage(tmp_path):
    from ironbench.report import aggregate

    records = [
        {"model": "m", "task": "t", "iterations": None, "duration_sec": None},
        {"model": "m", "task": "t", "iterations": "three", "duration_sec": {"bad": 1}},
    ]
    (stats,) = aggregate(records)
    assert stats.total_iterations == 0
    assert stats.total_duration == 0.0
