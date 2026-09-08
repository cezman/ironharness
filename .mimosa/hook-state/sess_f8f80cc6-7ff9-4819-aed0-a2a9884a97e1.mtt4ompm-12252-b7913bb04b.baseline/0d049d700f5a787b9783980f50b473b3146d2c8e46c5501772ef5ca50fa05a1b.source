"""Тесты Modbus TCP: симулятор как «железо», транспорт как агент (этап 1, задача 3)."""

import pytest

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
