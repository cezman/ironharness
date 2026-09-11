"""Тесты Modbus TCP: симулятор как «железо», транспорт как агент (этап 1, задача 3)."""

import pytest
from pymodbus.exceptions import ConnectionException

from io_core import JsonlJournal, ModbusTransport, read_events
from io_core.modbus_sim import ModbusSimServer


@pytest.fixture()
def sim():
    with ModbusSimServer(port=0, registers=[7, 8, 9] + [0] * 61) as srv:
        yield srv


def test_read_holding(sim):
    with ModbusTransport("127.0.0.1", port=sim.port) as t:
        assert t.read_holding(0, 3) == [7, 8, 9]


def test_write_single_then_read(sim):
    with ModbusTransport("127.0.0.1", port=sim.port) as t:
        t.write_register(5, 1234)
        assert t.read_holding(5) == [1234]


def test_write_block_then_read(sim):
    with ModbusTransport("127.0.0.1", port=sim.port) as t:
        t.write_registers(2, [11, 22, 33])
        assert t.read_holding(2, 3) == [11, 22, 33]


def test_out_of_range_raises(sim):
    with ModbusTransport("127.0.0.1", port=sim.port) as t, pytest.raises(IOError):
        t.read_holding(1000, 4)


def test_connection_refused_raises():
    with pytest.raises(ConnectionError):
        ModbusTransport("127.0.0.1", port=1, timeout=0.5).open()


def test_journal_records_modbus_ops(sim, tmp_path):
    jpath = tmp_path / "session.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, ModbusTransport(
        "127.0.0.1", port=sim.port, on_event=jr
    ) as t:
        t.write_registers(0, [42, 43])
        t.read_holding(0, 2)
    events = read_events(jpath)
    kinds = [e["kind"] for e in events]
    assert kinds == ["modbus_open", "modbus_write", "modbus_read", "modbus_close"]
    read_ev = events[2]
    assert read_ev["values"] == [42, 43]


def test_failed_read_is_journaled(sim, tmp_path):
    # IH-29: a failed operation is journaled before the exception escapes -
    # "no log = didn't happen" covers refusals, not only successes
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, ModbusTransport(
        "127.0.0.1", port=sim.port, on_event=jr
    ) as t, pytest.raises(OSError):
        t.read_holding(1000, 4)
    events = [e for e in read_events(jpath) if e["kind"] == "modbus_read_failed"]
    assert len(events) == 1
    assert events[0]["address"] == 1000 and "error" in events[0]


def test_failed_write_is_journaled(sim, tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, ModbusTransport(
        "127.0.0.1", port=sim.port, on_event=jr
    ) as t, pytest.raises(OSError):
        t.write_register(1000, 1)
    failed = [e for e in read_events(jpath) if e["kind"] == "modbus_write_failed"]
    assert len(failed) == 1
    assert failed[0]["values"] == [1]
    assert not [e for e in read_events(jpath) if e["kind"] == "modbus_write"]


def test_failed_open_is_journaled(tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(ConnectionError):
        ModbusTransport("127.0.0.1", port=1, timeout=0.5, on_event=jr).open()
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["modbus_open_failed"]
    assert events[0]["host"] == "127.0.0.1" and "error" in events[0]


def test_connection_exception_is_journaled(tmp_path):
    # pymodbus raises ConnectionException (not OSError) when the TCP link
    # drops mid-session - the most realistic failure must still be journaled
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        t = ModbusTransport("127.0.0.1", port=502, on_event=jr)
        t._client = _DroppingClient()
        with pytest.raises(ConnectionException):
            t.read_holding(0, 1)
    failed = [e for e in read_events(jpath) if e["kind"] == "modbus_read_failed"]
    assert len(failed) == 1 and "link down" in failed[0]["error"]


def test_failed_write_registers_is_journaled(sim, tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, ModbusTransport(
        "127.0.0.1", port=sim.port, on_event=jr
    ) as t, pytest.raises(OSError):
        t.write_registers(1000, [1, 2, 3])
    failed = [e for e in read_events(jpath) if e["kind"] == "modbus_write_failed"]
    assert len(failed) == 1 and failed[0]["values"] == [1, 2, 3]


class _DroppingClient:
    """Миниклиент: любая операция роняет линию (как умерший TCP-пир)."""

    def read_holding_registers(self, *a, **k):
        raise ConnectionException("link down")

    def write_register(self, *a, **k):
        raise ConnectionException("link down")

    def write_registers(self, *a, **k):
        raise ConnectionException("link down")
