"""Тесты сессии: именованные транспорты, песочница, единый журнал (этап 1, задача 7)."""

import pytest

from io_core import ModbusSimServer, SandboxViolation, Session, read_events


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
