"""Тесты serial-транспорта.

loop:// — программная петля pyserial, работает на любой ОС без железа.
pty-пара (только POSIX) — сценарий «два конца провода»: мастер играет роль
внешнего устройства, транспорт подключается к слейву как к настоящему порту.
"""

import os

import pytest
import serial

import io_core
from io_core import JsonlJournal, SerialTransport, read_events

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


# --- IH-29: a failed operation is journaled before the exception escapes ---


class _BoomPort:
    """A port object whose I/O always fails with the given exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.dtr = True
        self.rts = True

    def write(self, data):
        raise self._exc

    def read(self, size=1):
        raise self._exc

    def read_until(self, *args, **kwargs):
        raise self._exc

    def close(self):
        pass


def _patch_broken_port(monkeypatch, exc, *, fail_open=False):
    if fail_open:  # the failure happens inside serial_for_url itself
        monkeypatch.setattr(serial, "serial_for_url", lambda *a, **k: (_ for _ in ()).throw(exc))
    else:
        monkeypatch.setattr(serial, "serial_for_url", lambda *a, **k: _BoomPort(exc))


def test_failed_write_is_journaled(tmp_path, monkeypatch):
    _patch_broken_port(monkeypatch, serial.SerialTimeoutException("write timed out"))
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, SerialTransport(
        LOOP, on_event=jr
    ) as t, pytest.raises(serial.SerialTimeoutException):
        t.write(b"\xaa\xbb")
    events = read_events(jpath)
    # the context manager still closes the (broken) port on the way out
    assert [e["kind"] for e in events] == ["open", "write_failed", "close"]
    assert events[1]["data_hex"] == "aabb" and "timed out" in events[1]["error"]


def test_failed_read_is_journaled(tmp_path, monkeypatch):
    _patch_broken_port(monkeypatch, serial.SerialException("device gone"))
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, SerialTransport(
        LOOP, on_event=jr
    ) as t, pytest.raises(serial.SerialException):
        t.read(4)
    events = [e for e in read_events(jpath) if e["kind"] == "read_failed"]
    assert len(events) == 1 and events[0]["size"] == 4


def test_failed_open_is_journaled(tmp_path, monkeypatch):
    _patch_broken_port(monkeypatch, serial.SerialException("no such port"), fail_open=True)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(serial.SerialException):
        SerialTransport(LOOP, on_event=jr).open()
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["open_failed"]
    assert events[0]["port"] == LOOP and "error" in events[0]


def test_failed_open_bad_protocol_is_journaled(tmp_path):
    # unknown URL scheme -> ValueError from serial_for_url; the refusal is
    # still journaled (the port string comes from an untrusted agent)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(ValueError):
        SerialTransport("nosuchproto://x", on_event=jr).open()
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["open_failed"]


def test_failed_read_line_is_journaled(tmp_path, monkeypatch):
    _patch_broken_port(monkeypatch, serial.SerialException("device gone"))
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, SerialTransport(
        LOOP, on_event=jr
    ) as t, pytest.raises(serial.SerialException):
        t.read_line()
    failed = [e for e in read_events(jpath) if e["kind"] == "read_line_failed"]
    assert len(failed) == 1 and failed[0]["max_len"] == 256
