"""Тесты сессии: именованные транспорты, песочница, единый журнал (этап 1, задача 7)."""

import threading
import time

import pytest

from io_core import (
    ModbusSimServer,
    PolicyViolation,
    ReplayMismatch,
    ReplaySession,
    SandboxViolation,
    Session,
    read_events,
)
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
    # Два потока открывают одно имя: даже при медленном open() ровно один
    # успешен, второй получает KeyError (раньше оба проходили check_free,
    # открывались дважды и один транспорт терялся навсегда).
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


def test_typed_close_frees_name_even_when_close_fails(session, monkeypatch):
    session.serial_open("s", "loop://", timeout=0.5)
    t = session._transports["s"]
    monkeypatch.setattr(t, "close", lambda: (_ for _ in ()).throw(OSError("port stuck")))
    with pytest.raises(OSError):
        session.serial_close("s")
    session.serial_open("s", "loop://", timeout=0.5)  # имя освободилось


def test_session_close_survives_a_bad_port(session, monkeypatch):
    # Один зависший порт не должен оставить открытыми остальные и журнал:
    # close() собирает первую ошибку и перевыбрасывает её после зачистки.
    session.serial_open("bad", "loop://", timeout=0.5)
    session.serial_open("good", "loop://", timeout=0.5)
    bad = session._transports["bad"]
    monkeypatch.setattr(bad, "close", lambda: (_ for _ in ()).throw(OSError("stuck")))
    with pytest.raises(OSError):
        session.close()
    assert session._transports == {}  # реестр очищен, «good» не брошен
    with pytest.raises(ValueError):
        session.journal("late", {})  # журнал тоже закрыт
