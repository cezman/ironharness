"""Тесты serial-транспорта.

loop:// — программная петля pyserial, работает на любой ОС без железа.
pty-пара (только POSIX) — сценарий «два конца провода»: мастер играет роль
внешнего устройства, транспорт подключается к слейву как к настоящему порту.
"""

import os

import pytest

import io_core
from io_core import SerialTransport

LOOP = "loop://"


def test_loop_write_read_roundtrip():
    with SerialTransport(LOOP, timeout=0.5) as t:
        t.write(b"ping")
        assert t.read(4) == b"ping"


def test_loop_read_timeout_returns_empty():
    with SerialTransport(LOOP, timeout=0.2) as t:
        assert t.read(1) == b""


def test_loop_read_line():
    with SerialTransport(LOOP, timeout=0.5) as t:
        t.write(b"hello\n")
        assert t.read_line() == b"hello\n"


def test_events_hook_sequence():
    events: list[tuple[str, dict]] = []
    with SerialTransport(LOOP, timeout=0.5, on_event=lambda kind, data: events.append((kind, data))) as t:
        t.write(b"x")
        t.read(1)
    kinds = [kind for kind, _ in events]
    assert kinds == ["open", "write", "read", "close"]


def test_version_export():
    assert io_core.__version__


def test_open_deasserts_dtr_rts():
    # сим-vs-реал: при открытии линии сразу в idle, чтобы живая CH340-плата
    # не ловила импульс сброса от дефолтных DTR/RTS pyserial
    with SerialTransport(LOOP, timeout=0.2) as t:
        assert t._serial.dtr is False
        assert t._serial.rts is False


@pytest.mark.skipif(os.name != "posix", reason="pty-пары доступны только на Linux/macOS")
def test_pty_pair_device_side():
    import pty

    master, slave = pty.openpty()
    try:
        with SerialTransport(os.ttyname(slave), timeout=0.5) as t:
            os.write(master, b"from-device\n")
            assert t.read_line() == b"from-device\n"
            t.write(b"from-agent")
            assert os.read(master, 16) == b"from-agent"
    finally:
        os.close(master)
        os.close(slave)
