"""Тесты FaultInjectionTransport: детерминированные сбои на фейковом транспорте."""

from __future__ import annotations

import random

import pytest

from io_core.errors import ConnectionLost
from io_core.faults import Fault, FaultyTransport


class RecordingTransport:
    """Стаб: запоминает записи, отдаёт заготовленное чтение."""

    def __init__(self, read_data: bytes = b"ok"):
        self.writes: list[bytes] = []
        self.read_data = read_data
        self.reads = 0

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def read(self, size: int) -> bytes:
        self.reads += 1
        return self.read_data[:size]


def test_write_passthrough_without_faults():
    target = RecordingTransport()
    t = FaultyTransport(target, faults=[])
    t.write(b"ping")
    assert target.writes == [b"ping"]


def test_drop_write_silently_lost():
    target = RecordingTransport()
    t = FaultyTransport(target, faults=[Fault(action="drop")])
    t.write(b"lost")
    t.write(b"lost-again")
    assert target.writes == []  # probability=1: теряется каждая запись


def test_drop_after_ops_starts_later():
    target = RecordingTransport()
    t = FaultyTransport(target, faults=[Fault(action="drop", after_ops=1)])
    t.write(b"ok")  # операция 1 — сбой ещё не активен
    t.write(b"lost")  # операция 2 — активен
    assert target.writes == [b"ok"]


def test_corrupt_flips_bits_deterministically():
    target = RecordingTransport()
    rng = random.Random(42)
    t = FaultyTransport(target, faults=[Fault(action="corrupt", ratio=1.0)], rng=rng)
    t.write(b"AAAAAAAA")
    sent = target.writes[0]
    assert sent != b"AAAAAAAA"  # биты перевернулись
    assert len(sent) == 8  # длина не изменилась
    assert all((a ^ b).bit_count() == 1 for a, b in zip(b"AAAAAAAA", sent))


def test_corrupt_ratio_zero_keeps_data():
    target = RecordingTransport()
    t = FaultyTransport(target, faults=[Fault(action="corrupt", ratio=0.0)])
    t.write(b"clean")
    assert target.writes == [b"clean"]


def test_disconnect_raises_and_stops_reaching_target():
    target = RecordingTransport()
    # сбой только на третьей операции
    t = FaultyTransport(target, faults=[Fault(action="disconnect", after_ops=2)])
    t.write(b"1")
    t.write(b"2")
    with pytest.raises(ConnectionLost):
        t.write(b"3")
    assert target.writes == [b"1", b"2"]


def test_after_ops_gates_fault_activation():
    target = RecordingTransport()
    t = FaultyTransport(target, faults=[Fault(action="drop", after_ops=2)])
    t.write(b"a")
    t.write(b"b")
    t.write(b"c")  # операция 3 > after_ops=2 -> теряется
    assert target.writes == [b"a", b"b"]


def test_probability_zero_never_fires():
    target = RecordingTransport()
    t = FaultyTransport(target, faults=[Fault(action="drop", probability=0.0)])
    for _ in range(5):
        t.write(b"x")
    assert target.writes == [b"x"] * 5


def test_probability_one_always_fires():
    target = RecordingTransport()
    t = FaultyTransport(target, faults=[Fault(action="drop", probability=1.0)])
    t.write(b"x")
    assert target.writes == []


def test_delay_calls_injected_sleep():
    target = RecordingTransport()
    slept: list[float] = []
    t = FaultyTransport(
        target, faults=[Fault(action="delay", seconds=1.5)], sleep=slept.append
    )
    t.write(b"y")
    t.read(2)
    assert slept == [1.5, 1.5]  # и на write, и на read


def test_read_drop_returns_empty_without_target():
    target = RecordingTransport()
    t = FaultyTransport(target, faults=[Fault(action="drop")])
    assert t.read(10) == b""
    assert target.reads == 0


def test_read_passthrough():
    target = RecordingTransport(read_data=b"hello")
    t = FaultyTransport(target, faults=[])
    assert t.read(5) == b"hello"


def test_fault_validation():
    with pytest.raises(ValueError, match="действие"):
        Fault(action="explode")
    with pytest.raises(ValueError, match="probability"):
        Fault(action="drop", probability=2.0)
    with pytest.raises(ValueError, match="ratio"):
        Fault(action="corrupt", ratio=-0.1)


def test_fault_from_dict():
    target = RecordingTransport()
    t = FaultyTransport(target, faults=[{"action": "drop"}])
    t.write(b"x")
    assert target.writes == []


def test_attribute_delegation():
    target = RecordingTransport(read_data=b"ok")
    t = FaultyTransport(target, faults=[])
    assert t.read_data == b"ok"


def test_count_limits_fault_window():
    # окно активности (after_ops, after_ops+count]: сбой бьёт ровно одну операцию
    target = RecordingTransport()
    t = FaultyTransport(
        target,
        faults=[Fault("drop", after_ops=1, count=1)],
        rng=random.Random(1),
    )
    for chunk in (b"one", b"two", b"three", b"four"):
        t.write(chunk)
    assert target.writes == [b"one", b"three", b"four"]


def test_count_validation():
    with pytest.raises(ValueError, match="count"):
        Fault("drop", count=0)
