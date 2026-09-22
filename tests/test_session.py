"""Тесты сессии: именованные транспорты, песочница, единый журнал (этап 1, задача 7)."""

import threading
import time

import pytest

from io_core import (
    DeadlineTransport,
    ModbusSimServer,
    OperationTimeout,
    PolicyViolation,
    RateLimitExceeded,
    ReplayMismatch,
    ReplaySession,
    SandboxViolation,
    SerialTransport,
    Session,
    TransportClosedError,
    read_events,
)
from io_core.limits import parse_transport_deadline, parse_transport_rate
from io_core.mqtt_sim import MqttSimBroker


@pytest.fixture()
def session(tmp_path):
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    yield s
    s.close()


def test_serial_roundtrip_via_session(session):
    session.serial_open("s", "loop://", timeout=0.5)
    session.serial_write("s", "cafebabe")
    assert session.serial_read("s", 4) == "cafebabe"


def test_unknown_transport_raises(session):
    with pytest.raises(KeyError):
        session.serial_read("nope", 1)


def test_duplicate_open_raises(session):
    session.serial_open("s", "loop://", timeout=0.5)
    with pytest.raises(KeyError):
        session.serial_open("s", "loop://", timeout=0.5)


def test_typed_close_releases_transport(session):
    session.serial_open("s", "loop://", timeout=0.5)
    session.serial_close("s")
    with pytest.raises(KeyError):
        session.serial_write("s", "00")
    session.serial_open("s", "loop://", timeout=0.5)  # имя освободилось


def test_typed_close_rejects_wrong_kind(session):
    session.serial_open("s", "loop://", timeout=0.5)
    with pytest.raises(KeyError):
        session.modbus_close("s")


def test_modbus_via_session(session):
    with ModbusSimServer(port=0, registers=[5, 6] + [0] * 62) as srv:
        session.modbus_open("m", "127.0.0.1", port=srv.port)
        session.modbus_write("m", 0, [5, 6])
        assert session.modbus_read("m", 0, 2) == [5, 6]


def test_single_modbus_write_uses_fc6(session):
    with ModbusSimServer(port=0, registers=[0] * 64) as srv:
        session.modbus_open("m", "127.0.0.1", port=srv.port)
        session.modbus_write("m", 7, [99])
        assert session.modbus_read("m", 7) == [99]


def test_file_ops_via_session(session):
    session.file_write("dir/f.txt", "привет")
    assert session.file_read("dir/f.txt") == "привет"
    assert session.file_list() == ["dir", "dir/f.txt"]
    session.file_delete("dir/f.txt")
    assert session.file_list() == ["dir"]


def test_file_escape_via_session_blocked(session):
    with pytest.raises(SandboxViolation):
        session.file_write("../x.txt", "no")


def test_all_operations_in_one_journal(session, tmp_path):
    with ModbusSimServer(port=0, registers=[0] * 64) as srv:
        session.serial_open("s", "loop://", timeout=0.5)
        session.serial_write("s", "01")
        session.modbus_open("m", "127.0.0.1", port=srv.port)
        session.modbus_read("m", 0)
        session.file_write("f.txt", "data")
    events = read_events(tmp_path / "journal.jsonl")
    kinds = [e["kind"] for e in events]
    # serial-открытие, запись, modbus-открытие, чтение, запись файла — всё в одном журнале
    for expected in ("open", "write", "modbus_open", "modbus_read", "file_write"):
        assert expected in kinds


def test_close_transport_removes_name(session):
    session.serial_open("s", "loop://", timeout=0.5)
    session.close_transport("s")
    with pytest.raises(KeyError):
        session.serial_read("s", 1)
    session.serial_open("s", "loop://", timeout=0.5)  # имя освободилось
    session.close_transport("s")
    assert session._kinds == {}  # IH-17: kind-реестр синхронен с реестром транспортов


def test_serial_list_journals_and_formats(tmp_path, monkeypatch):
    # IH-36: MCP-first port enumeration - vid/pid as 4-hex-digit USB IDs,
    # None when the port has no USB identity, journaled like every operation.
    import serial.tools.list_ports as lp

    class FakePort:
        device = "COM6"
        vid = 0x86  # deliberately < 0x1000: the 4-hex-digit padding is pinned below
        pid = 0x23
        description = "USB-SERIAL CH340"
        serial_number = None
        location = "1-3"

    class FakePortNoId:
        device = "COM1"
        vid = None
        pid = None
        description = None
        serial_number = None
        location = None

    monkeypatch.setattr(lp, "comports", lambda: [FakePort(), FakePortNoId()])
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    try:
        ports = s.serial_list()
    finally:
        s.close()
    assert ports[0] == {
        "device": "COM6",
        "vid": "0086",
        "pid": "0023",
        "serial_number": None,
        "location": "1-3",
        "description": "USB-SERIAL CH340",
    }
    assert ports[1] == {
        "device": "COM1",
        "vid": None,
        "pid": None,
        "serial_number": None,
        "location": None,
        "description": None,
    }
    listed = [e for e in read_events(tmp_path / "journal.jsonl") if e["kind"] == "serial_listed"]
    assert listed and listed[0]["count"] == 2


def test_serial_list_respects_enabled_kinds(tmp_path, monkeypatch):
    # serial_list is gated by the serial kind like the rest of the family
    monkeypatch.setenv("IRONHARNESS_ENABLED_KINDS", "modbus")
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    try:
        with pytest.raises(PolicyViolation):
            s.serial_list()
    finally:
        s.close()


def test_modbus_ops_respect_deadline(session, monkeypatch):
    # IH-17: лимиты действуют и на forwarded-операции (modbus_read идёт через
    # __getattr__ обёртки), а не только на serial write/read
    monkeypatch.setenv("IRONHARNESS_TRANSPORT_DEADLINE", "1")
    with ModbusSimServer(port=0, registers=[0] * 64) as srv:
        session.modbus_open("m", "127.0.0.1", port=srv.port)
        assert session.modbus_read("m", 0) == [0]  # внутри дедлайна
        time.sleep(1.3)
        with pytest.raises(OperationTimeout):
            session.modbus_read("m", 0)


def test_modbus_ops_respect_rate(session, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_TRANSPORT_RATE", "2/60")
    with ModbusSimServer(port=0, registers=[0] * 64) as srv:
        session.modbus_open("m", "127.0.0.1", port=srv.port)
        assert session.modbus_read("m", 0) == [0]
        assert session.modbus_read("m", 0) == [0]
        with pytest.raises(RateLimitExceeded):
            session.modbus_read("m", 0)


def test_mqtt_ops_respect_rate(session, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_TRANSPORT_RATE", "2/60")
    broker = MqttSimBroker()
    port = broker.start()
    try:
        session.mqtt_open("q", "127.0.0.1", port=port)
        session.mqtt_publish("q", "dev/t", "a")
        session.mqtt_publish("q", "dev/t", "b")
        with pytest.raises(RateLimitExceeded):
            session.mqtt_publish("q", "dev/t", "c")
    finally:
        broker.stop()


# --- IH-11: журнал атрибутирует операции соединениям, реплей по-соединений ---


def test_journal_events_carry_connection_name(session, tmp_path):
    session.serial_open("a", "loop://", timeout=0.5)
    session.serial_open("b", "loop://", timeout=0.5)
    session.serial_write("a", "01")
    session.serial_write("b", "02")
    session.serial_read("a", 1)
    events = read_events(tmp_path / "journal.jsonl")
    ops = [(e["kind"], e["conn"]) for e in events if e["kind"] in ("write", "read")]
    assert ops == [("write", "a"), ("write", "b"), ("read", "a")]


def test_replay_per_connection(session, tmp_path):
    session.serial_open("a", "loop://", timeout=0.5)
    session.serial_open("b", "loop://", timeout=0.5)
    session.serial_write("a", b"ping-a".hex())
    assert session.serial_read("a", 6) == b"ping-a".hex()
    session.serial_write("b", b"ping-b".hex())
    assert session.serial_read("b", 6) == b"ping-b".hex()

    rs = ReplaySession.from_file(tmp_path / "journal.jsonl")
    assert rs.order == ("a", "b")
    ra, rb = rs["a"], rs["b"]
    with ra, rb:
        ra.write(b"ping-a")
        assert ra.read(6) == b"ping-a"
        rb.write(b"ping-b")
        assert rb.read(6) == b"ping-b"
        # ответы «b» не утекают в поток «a»
        assert ra.read(6) == b""
        # strict-сверка записей — на соединение: команда «b» в «a» не проходит
        with pytest.raises(ReplayMismatch):
            ra.write(b"ping-b")


def test_journal_conn_covers_modbus_and_mqtt(session, tmp_path):
    # хук с conn получает каждый транспорт, включая mqtt (события из сетевого
    # потока paho идут через тот же _journal_for)
    broker = MqttSimBroker()
    port = broker.start()
    try:
        with ModbusSimServer(port=0, registers=[0] * 64) as srv:
            session.modbus_open("m", "127.0.0.1", port=srv.port)
            session.modbus_read("m", 0)
            session.mqtt_open("q", "127.0.0.1", port=port)
            session.mqtt_publish("q", "dev1/value", "1")
    finally:
        broker.stop()
    events = read_events(tmp_path / "journal.jsonl")
    conns = {(e["kind"], e["conn"]) for e in events}
    assert ("modbus_read", "m") in conns
    assert ("mqtt_publish", "q") in conns


# --- IH-12: атомарность реестра и квот, предел соединений, живучий close ---


def test_duplicate_open_race_is_atomic(session, monkeypatch):
    # Two threads opening one name: even with a slow open() exactly one wins,
    # the other gets KeyError. Before IH-12 both passed the free-check, opened
    # twice and one transport was lost forever (leaked past session.close()).
    import io_core.session as session_module

    real_cls = session_module.SerialTransport

    class SlowSerial(real_cls):
        def open(self):
            time.sleep(0.2)
            super().open()

    monkeypatch.setattr(session_module, "SerialTransport", SlowSerial)
    errors: list[KeyError] = []

    def worker():
        try:
            session.serial_open("s", "loop://", timeout=0.5)
        except KeyError as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 1
    assert len(session._transports) == 1


def test_connection_limit_policy(session, tmp_path, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_MAX_CONNECTIONS", "1")
    session.serial_open("a", "loop://", timeout=0.5)
    with pytest.raises(PolicyViolation):
        session.serial_open("b", "loop://", timeout=0.5)
    events = read_events(tmp_path / "journal.jsonl")
    assert any(
        e["kind"] == "policy_violation" and e.get("rule") == "max_connections" for e in events
    )


def test_connection_limit_rejects_garbage(session, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_MAX_CONNECTIONS", "two")
    with pytest.raises(ValueError):
        session.serial_open("a", "loop://", timeout=0.5)


def test_connection_limit_zero_denies_all_opens(session, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_MAX_CONNECTIONS", "0")
    with pytest.raises(PolicyViolation):
        session.serial_open("a", "loop://", timeout=0.5)


def test_typed_close_frees_name_even_when_close_fails(session, monkeypatch):
    session.serial_open("s", "loop://", timeout=0.5)
    t = session._transports["s"]
    monkeypatch.setattr(t, "close", lambda: (_ for _ in ()).throw(OSError("port stuck")))
    with pytest.raises(OSError):
        session.serial_close("s")
    session.serial_open("s", "loop://", timeout=0.5)  # the name is free again


def test_session_close_survives_a_bad_port(session, monkeypatch):
    # One stuck port must not leak the rest: close() clears the registry, shuts
    # the journal down and only then re-raises the first collected error.
    session.serial_open("bad", "loop://", timeout=0.5)
    session.serial_open("good", "loop://", timeout=0.5)
    bad = session._transports["bad"]
    monkeypatch.setattr(bad, "close", lambda: (_ for _ in ()).throw(OSError("stuck")))
    with pytest.raises(OSError):
        session.close()
    assert session._transports == {}  # registry cleared, "good" not abandoned
    with pytest.raises(ValueError):
        session.journal("late", {})  # the journal is closed too


# --- IH-17: the convention 'transports always have timeouts and quotas' is
# --- enforced by the standard session itself (deadline on by default) ---


def test_default_deadline_wraps_transports(session):
    session.serial_open("s", "loop://", timeout=0.5)
    wrapped = session._transports["s"]
    assert isinstance(wrapped, DeadlineTransport)  # deadline on by default
    # operations still work through the wrapper (getattr forwarding)
    session.serial_write("s", "cafe")
    assert session.serial_read("s", 2) == "cafe"


def test_deadline_expires_produces_timeout(session, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_TRANSPORT_DEADLINE", "1")
    session.serial_open("s", "loop://", timeout=0.5)
    time.sleep(1.3)
    with pytest.raises(OperationTimeout):
        session.serial_read("s", 1)


def test_deadline_denial_names_connection(session, monkeypatch, tmp_path):
    # IH-35: the timeout text names the stale connection - an agent with two
    # open transports can tell which one to reopen without guessing
    monkeypatch.setenv("IRONHARNESS_TRANSPORT_DEADLINE", "1")
    session.serial_open("s", "loop://", timeout=0.5)
    time.sleep(1.3)
    with pytest.raises(OperationTimeout, match=r"'s'"):
        session.serial_read("s", 1)
    # the denial is journaled with the conn stamp (IH-35 closes the missing-name gap)
    events = [
        e for e in read_events(tmp_path / "journal.jsonl") if e.get("kind") == "deadline_denied"
    ]
    assert events and events[-1]["conn"] == "s"


def test_deadline_slides_across_session_ops(session, monkeypatch):
    # IH-35: a sparse-but-alive interactive session survives - ops 0.7s apart
    # on a 1s deadline (cumulative 1.4s); lifetime semantics killed the third
    # write, the sliding window condemns only true silence.
    monkeypatch.setenv("IRONHARNESS_TRANSPORT_DEADLINE", "1")
    session.serial_open("s", "loop://", timeout=0.5)
    session.serial_write("s", "aa")
    time.sleep(0.7)
    session.serial_write("s", "bb")
    time.sleep(0.7)
    session.serial_write("s", "cc")  # 1.4s since open, 0.7s idle: passes
    time.sleep(1.3)
    with pytest.raises(OperationTimeout):
        session.serial_read("s", 1)  # 1.3s of silence: denied


def test_deadline_disabled_by_env(session, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_TRANSPORT_DEADLINE", "off")
    session.serial_open("s", "loop://", timeout=0.5)
    assert type(session._transports["s"]) is SerialTransport


def test_deadline_garbage_env_fails_loud(session, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_TRANSPORT_DEADLINE", "-5")
    with pytest.raises(ValueError):
        session.serial_open("s", "loop://", timeout=0.5)


def test_rate_limit_env_enforced(session, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_TRANSPORT_RATE", "3/60")
    session.serial_open("s", "loop://", timeout=0.5)
    for _ in range(3):
        session.serial_write("s", "00")
    with pytest.raises(RateLimitExceeded):
        session.serial_write("s", "00")


def test_rate_limit_env_unset_means_no_limit(session):
    session.serial_open("s", "loop://", timeout=0.5)
    for _ in range(10):
        session.serial_write("s", "00")  # no exception without the env


def test_parse_transport_deadline_and_rate():
    assert parse_transport_deadline(None) == 600.0
    assert parse_transport_deadline("") == 600.0
    assert parse_transport_deadline("30") == 30.0
    assert parse_transport_deadline("off") is None
    assert parse_transport_deadline("0") is None
    with pytest.raises(ValueError):
        parse_transport_deadline("-1")
    with pytest.raises(ValueError):
        parse_transport_deadline("nan")  # a nan comparison silently disables the deadline
    with pytest.raises(ValueError):
        parse_transport_deadline("inf")
    assert parse_transport_rate(None) is None
    assert parse_transport_rate("100/60") == (100, 60.0)
    with pytest.raises(ValueError):
        parse_transport_rate("100")
    with pytest.raises(ValueError):
        parse_transport_rate("0/60")
    with pytest.raises(ValueError):
        parse_transport_rate("1/inf")


# --- IH-73: serial_wait obeys the session lifecycle and the kind policy ---

def test_serial_wait_refuses_closed_session(tmp_path):
    # IH-73: serial_wait polled the host ports even on a closed session and
    # ended in TimeoutError - the gate must refuse immediately, like the
    # other serial tools
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    s.close()
    start = time.monotonic()
    with pytest.raises(TransportClosedError):
        # an address that never matches a real host port, so the test does
        # not depend on what is plugged into the machine
        s.serial_wait("ffff", "ffff", timeout=30)
    assert time.monotonic() - start < 5  # refused, not polled to the timeout


def test_serial_wait_respects_enabled_kinds(tmp_path, monkeypatch):
    # IH-73: with the serial kind disabled by policy, serial_wait must not
    # enumerate ports - the policy violation is journaled like everywhere
    monkeypatch.setenv("IRONHARNESS_ENABLED_KINDS", "modbus")
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        with pytest.raises(PolicyViolation):
            s.serial_wait("1a86", "7523", timeout=1)
        kinds = [e["kind"] for e in read_events(tmp_path / "j.jsonl")]
        assert "policy_violation" in kinds
    finally:
        s.close()


def test_serial_wait_timeout_is_journaled(tmp_path):
    # IH-51 convention: an outcome that surfaces to the agent as an error
    # is journaled - the timeout of an unsuccessful wait was silent
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        with pytest.raises(TimeoutError):
            s.serial_wait("ffff", "ffff", timeout=0.6)
        kinds = [e["kind"] for e in read_events(tmp_path / "j.jsonl")]
        assert "serial_wait_timeout" in kinds
    finally:
        s.close()


def test_serial_open_by_serial_resolves_after_gates(tmp_path):
    # IH-73 class fix (review): the by-serial: resolution enumerates the host
    # port table and must not run past the lifecycle gate - on a closed
    # session it used to raise ValueError from the resolver instead of the
    # typed closed-session error
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    s.close()
    with pytest.raises(TransportClosedError):
        s.serial_open("s", "by-serial:no-such-serial")


def test_transport_tools_without_conn_declare_lifecycle_gate():
    # IH-73 class sweep: the tools that operate without a transport name
    # (pure host enumeration) must still check the session lifecycle at
    # entry; status/close are deliberate exemptions (recovery/teardown).
    # Tools taking a conn name go through _get() - their closed-session
    # behavior is the registry KeyError, a separate (pre-existing) class.
    import inspect

    gated = ["serial_list", "serial_wait"]
    missing = [
        name
        for name in gated
        if "_check_open()" not in inspect.getsource(getattr(Session, name))
    ]
    assert not missing, f"transport tools without _check_open(): {missing}"
    # both enumeration paths declare the serial kind policy
    assert '_check_kind("serial")' in inspect.getsource(Session.serial_list)
    assert '_check_kind("serial")' in inspect.getsource(Session.serial_wait)


# --- IH-78: MCP tool arguments are bounded, VID/PID match is case-insensitive ---


def test_serial_read_rejects_insane_size(tmp_path):
    # IH-78 (review): size=10**9 made pyserial pre-allocate a gigabyte buffer
    # - arguments get upper bounds like every other layer
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        s.serial_open("s", "loop://", timeout=0.5)
        with pytest.raises(ValueError):
            s.serial_read("s", size=10**9)
    finally:
        s.close()


def test_serial_wait_rejects_zero_timeout(tmp_path):
    # IH-78 (review): timeout is bounded to (0, 3600] seconds
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        with pytest.raises(ValueError):
            s.serial_wait("ffff", "ffff", timeout=0)
    finally:
        s.close()


class _FakePort:
    def __init__(self, vid, pid, device, serial_number=""):
        self.vid, self.pid, self.device, self.serial_number = vid, pid, device, serial_number


def test_serial_wait_vidpid_case_insensitive(tmp_path, monkeypatch):
    # IH-78 (review): vid_s is lowercase hex; a natural "1A86" from the agent
    # used to never match and the call waited out the whole timeout
    import serial.tools.list_ports

    monkeypatch.setattr(
        serial.tools.list_ports,
        "comports",
        lambda: [_FakePort(0x1A86, 0x7523, "COMX", serial_number="TEST")],
    )
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        res = s.serial_wait("1A86", "7523", timeout=1)
        assert res["device"] == "COMX"
    finally:
        s.close()


def test_open_timeout_bounds_gated_and_journaled(tmp_path):
    # IH-78 review: the open-timeout class parks workers on a silent host -
    # each refusal is journaled (no log = didn't happen)
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        with pytest.raises(ValueError):
            s.serial_open("s", "loop://", timeout=0)
        with pytest.raises(ValueError):
            s.modbus_open("m", "127.0.0.1", timeout=0)
        with pytest.raises(ValueError):
            s.mqtt_open("q", "127.0.0.1", timeout=0)
        kinds = [e["kind"] for e in read_events(tmp_path / "j.jsonl")]
        for kind in ("serial_open_refused", "modbus_open_refused", "mqtt_open_refused"):
            assert kind in kinds
    finally:
        s.close()


def test_serial_reader_start_max_bytes_gated(tmp_path):
    # IH-78 review: the reader buffer growth cap is gated at the session
    # level; the lower bound matches the reader's own chunk minimum (512)
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        for bad in (0, 17_000_000):
            with pytest.raises(ValueError):
                s.serial_reader_start("s", max_bytes=bad)
        kinds = [e["kind"] for e in read_events(tmp_path / "j.jsonl")]
        assert kinds.count("reader_start_refused") == 2
    finally:
        s.close()


def test_arg_bound_refusals_are_journaled(tmp_path):
    # IH-78 review: every bound refusal leaves a JSONL trace with a reason
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        bad_calls = [
            (s.serial_read, {"size": 10**9}),
            (s.serial_read_line, {"max_len": 0}),
            (s.serial_tail, {"size": 0}),
            (s.serial_read_until, {"pattern": "x", "timeout": 0}),
            (s.mqtt_read, {"timeout": 0}),
            (s.serial_wait, {"pid": "ffff", "timeout": 0}),
        ]
        for fn, kwargs in bad_calls:
            with pytest.raises(ValueError):
                fn("x", **kwargs)
        kinds = [e["kind"] for e in read_events(tmp_path / "j.jsonl")]
        for kind in (
            "read_refused",
            "read_line_refused",
            "tail_refused",
            "read_until_refused",
            "mqtt_read_refused",
            "wait_refused",
        ):
            assert kind in kinds, kind
    finally:
        s.close()


# --- IH-77: esp_image_info is gated by the sandbox / the real-flash opt-in ---


def test_esp_image_info_rejects_paths_outside_sandbox(tmp_path, monkeypatch):
    # IH-77 (review): esp_image_info parsed any host path without the sandbox
    # or the real-flash gate - existence/size/segments of any file, journal
    # included. Outside paths are refused (journaled) unless the operator
    # opts in with IRONHARNESS_ALLOW_REAL_FLASH=1
    monkeypatch.delenv("IRONHARNESS_ALLOW_REAL_FLASH", raising=False)
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        with pytest.raises(PolicyViolation):
            s.esp_image_info(str(tmp_path / "outside.bin"))
        kinds = [e["kind"] for e in read_events(tmp_path / "j.jsonl")]
        assert "esp_denied" in kinds
    finally:
        s.close()


def test_esp_image_info_gate_lifted_by_real_flash_env(tmp_path, monkeypatch):
    # with the opt-in the gate passes the (absolute) path through; the
    # parser itself is stubbed - this test pins the gate, not esptool
    from io_core import esp_flash

    monkeypatch.setenv("IRONHARNESS_ALLOW_REAL_FLASH", "1")
    seen = {}

    def fake_image_info(self, firmware_path):
        seen["path"] = firmware_path
        return {"ok": True}

    monkeypatch.setattr(esp_flash.EspFlasher, "image_info", fake_image_info)
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"\x00")
        assert s.esp_image_info(str(outside)) == {"ok": True}
        assert seen["path"] == str(outside)
    finally:
        s.close()


def test_esp_flash_firmware_path_is_gated_to_sandbox(tmp_path, monkeypatch):
    # IH-113: the destructive flash must not be gated looser than the offline
    # parser - a host path outside the sandbox is refused (journaled) unless
    # the operator lifts it with the real-flash opt-in
    monkeypatch.delenv("IRONHARNESS_ALLOW_REAL_FLASH", raising=False)
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        (tmp_path / "outside.bin").write_bytes(b"\x00")
        with pytest.raises(PolicyViolation):
            s.esp_flash("COM7", str(tmp_path / "outside.bin"))
        denials = [
            e
            for e in read_events(tmp_path / "j.jsonl")
            if e["kind"] == "esp_denied" and e["op"] == "flash"
        ]
        assert denials, "the flash path refusal passed unjournaled"
    finally:
        s.close()


def test_esp_flash_sandbox_gate_lifted_by_real_flash_env(tmp_path, monkeypatch):
    # the counter-negative: with the opt-in the gate passes the resolved host
    # path through; the flasher itself is stubbed - the gate is the subject
    from io_core import esp_flash

    monkeypatch.setenv("IRONHARNESS_ALLOW_REAL_FLASH", "1")
    seen = {}

    def fake_flash(self, port, firmware_path, *, addr=0x1000, baud=921600):
        seen["path"] = firmware_path
        return "ok"

    monkeypatch.setattr(esp_flash.EspFlasher, "flash", fake_flash)
    s = Session(tmp_path / "j.jsonl", tmp_path / "sandbox", actor="test")
    try:
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"\x00")
        assert s.esp_flash("COM7", str(outside)) == "ok"
        assert seen["path"] == str(outside)
    finally:
        s.close()


# --- audit D (2026-09-20): the surface tools get executed tests ---


def test_status_empty_session_structure(session):
    # audit D2: session_status is the self-recovery tool - its dict shape on
    # a fresh session is pinned by calling it, not by trusting the wire
    st = session.status()
    assert st == {
        "transports": {},
        "readers": {},
        "transfers": [],
        "monitors": [],  # IH-83: running serial_monitor windows
        "sandbox": str(session.sandbox.root),
    }


def test_status_reflects_serial_reader_and_transfer(session, monkeypatch):
    session.serial_open("s", "loop://", timeout=0.5)
    session.serial_reader_start("s")
    monkeypatch.setattr(session, "_transfers", {"fake-put"}, raising=False)
    st = session.status()
    assert st["transports"]["s"]["kind"] == "serial"
    assert st["transports"]["s"]["port"] == "loop://"
    assert st["readers"]["s"]["alive"] is True
    assert st["readers"]["s"]["buffered"] >= 0
    assert st["transfers"] == ["fake-put"]
    assert st["sandbox"] == str(session.sandbox.root)


def test_serial_open_by_serial_resolves_device(session, monkeypatch):
    # audit D3 (positive): by-serial resolves the floating COM number from
    # the USB serial_number, case-insensitively
    import io_core.session as session_module

    class FakePort:
        device = "loop://"
        serial_number = "555ABBA"
        vid = 0x1A86
        pid = 0x7523
        description = "fake bench"
        location = "1-1"

    monkeypatch.setattr(
        session_module.serial.tools.list_ports, "comports", lambda: [FakePort()]
    )
    session.serial_open("board", "by-serial:5abba", timeout=0.5)
    assert session.status()["transports"]["board"]["port"] == "loop://"


def test_serial_open_by_serial_unknown_hints_serial_list(session, monkeypatch):
    # audit D3 (negative): an unknown serial_number raises with the
    # self-recovery hint instead of a bare error
    import io_core.session as session_module

    monkeypatch.setattr(session_module.serial.tools.list_ports, "comports", list)
    with pytest.raises(ValueError, match="serial_list"):
        session.serial_open("board", "by-serial:NOPE", timeout=0.5)
