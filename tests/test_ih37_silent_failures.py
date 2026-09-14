"""IH-37: the silent-failures tail - every failed I/O operation must land in
the journal ("no log = didn't happen" covers refusals). Covers the classes
from the IH-34 review: modbus struct/ValueError past the OSError handlers,
file_sandbox OSError paths, esp_flash ImportError/parse failures past the
gate, mqtt open ValueError, serial_write on a closed transport, and the
serial_put/get reader-gate TOCTOU.
"""

from __future__ import annotations

import struct
import threading
from pathlib import Path

import pytest

from io_core.errors import TransportClosedError
from io_core.file_sandbox import FileSandbox
from io_core.journal import read_events
from io_core.modbus_transport import ModbusTransport
from io_core.mqtt_transport import MqttTransport
from io_core.session import Session


def kinds(journal_path: Path) -> list[str]:
    return [e["kind"] for e in read_events(journal_path)]


class Journaling:
    def __init__(self, tmp_path: Path, name: str) -> None:
        self.path = tmp_path / f"{name}.jsonl"
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind: str, data: dict) -> None:
        self.events.append((kind, data))


# --- modbus: non-OSError/ModbusException failures must journal ---


class FakeModbusClient:
    """Duck-typed pymodbus client: the op raises the configured exception."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def connect(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def read_holding_registers(self, *a, **k):
        raise self.exc

    def write_register(self, *a, **k):
        raise self.exc

    def write_registers(self, *a, **k):
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "failed_kind"),
    [
        (struct.error("bad payload"), "modbus_read_failed"),
        (ValueError("bad argument"), "modbus_read_failed"),
    ],
)
def test_modbus_read_failure_journaled_for_non_oserror(
    tmp_path, monkeypatch, exc, failed_kind
):
    import io_core.modbus_transport as mt

    j = Journaling(tmp_path, "mb")
    t = ModbusTransport("127.0.0.1", on_event=j)
    monkeypatch.setattr(mt, "ModbusTcpClient", lambda *a, **k: FakeModbusClient(exc))
    t.open()
    with pytest.raises(type(exc)):
        t.read_holding(0)
    assert failed_kind in [k for k, _ in j.events], (
        f"a non-OSError modbus failure passed unjournaled: {[k for k, _ in j.events]}"
    )


def test_modbus_closed_transport_is_transportclosederror_and_journaled(tmp_path):
    j = Journaling(tmp_path, "mb2")
    t = ModbusTransport("127.0.0.1", on_event=j)
    with pytest.raises(TransportClosedError):
        t.read_holding(0)
    assert "modbus_read_failed" in [k for k, _ in j.events]


# --- file_sandbox: OSError paths must journal *_failed ---


def test_file_sandbox_failures_journaled(tmp_path):
    j = Journaling(tmp_path, "fs")
    sb = FileSandbox(tmp_path / "sb", on_event=j)

    with pytest.raises(OSError):
        sb.read_file("missing.txt")
    with pytest.raises(OSError):
        sb.delete_file("missing.txt")
    sb.write_file("exists.txt", b"x")
    with pytest.raises(OSError):
        sb.write_file("exists.txt", b"y")
    with pytest.raises(OSError):
        sb.list_dir("exists.txt")

    ks = [k for k, _ in j.events]
    for kind in ("file_read_failed", "file_delete_failed", "file_write_failed", "file_list_failed"):
        assert kind in ks, f"{kind} missing - a silent sandbox failure: {ks}"


# --- esp_flash: ImportError and parse failures must journal ---


def test_esp_image_info_import_error_journaled(tmp_path, monkeypatch):
    from io_core import esp_flash

    j = Journaling(tmp_path, "esp")
    fw = tmp_path / "fw.bin"
    fw.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(esp_flash, "_ESPTOOL_AVAILABLE", False)
    f = esp_flash.EspFlasher(on_event=j)
    with pytest.raises(ImportError):
        f.image_info(fw)
    assert "esp_image_info_failed" in [k for k, _ in j.events]


def test_esp_image_info_parse_error_journaled(tmp_path, monkeypatch):
    from io_core import esp_flash

    j = Journaling(tmp_path, "esp2")
    fw = tmp_path / "fw.bin"
    fw.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(esp_flash, "_ESPTOOL_AVAILABLE", True)

    def boom(*a, **k):
        raise ValueError("not an esp image")

    monkeypatch.setattr(esp_flash, "LoadFirmwareImage", boom)
    f = esp_flash.EspFlasher(on_event=j)
    with pytest.raises(ValueError):
        f.image_info(fw)
    assert "esp_image_info_failed" in [k for k, _ in j.events]


# --- mqtt: non-OSError open failures must journal ---


def test_mqtt_open_value_error_journaled(tmp_path):
    j = Journaling(tmp_path, "mqtt")

    def factory():
        client = FakeMqttClient()
        return client

    class FakeMqttClient:
        def connect(self, host, port, keepalive):
            raise ValueError("invalid port")

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

    t = MqttTransport("127.0.0.1", on_event=j, client_factory=factory)
    with pytest.raises(ValueError):
        t.open()
    assert "mqtt_open_failed" in [k for k, _ in j.events], (
        f"mqtt open ValueError passed unjournaled: {[k for k, _ in j.events]}"
    )


# --- session: serial_write on a closed transport must journal ---


def test_serial_write_closed_transport_journaled(tmp_path):
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("loop", "loop://")
        s.close_transport("loop")
        with pytest.raises(KeyError):
            s.serial_write("loop", b"x".hex())
        ks = kinds(tmp_path / "j.jsonl")
        assert "write_failed" in ks, f"closed-transport write passed unjournaled: {ks}"
    finally:
        s.close()


# --- the reader gate TOCTOU: reader cannot start mid-transfer ---


def test_reader_cannot_start_during_transfer(tmp_path, monkeypatch):
    """IH-37: the reader gate in serial_put checked under the session lock,
    but the transfer ran unlocked - serial_reader_start could start a reader
    mid-transfer (check-then-act). The gate must be symmetric."""
    s = Session(tmp_path / "j.jsonl", tmp_path / "sb", actor="test")
    try:
        s.serial_open("loop", "loop://")
        (tmp_path / "sb" / "src.txt").write_bytes(b"x")  # inside the sandbox root
        release = threading.Event()
        started = threading.Event()

        def slow_put(t, data, target_path):
            started.set()
            release.wait(timeout=10)
            return len(data)

        from io_core import mprepl

        monkeypatch.setattr(mprepl, "put_file", slow_put)
        result: dict = {}

        def do_put():
            try:
                s.serial_put("loop", "src.txt", "dst.txt")
            except BaseException as e:  # noqa: BLE001
                result["error"] = e

        th = threading.Thread(target=do_put)
        th.start()
        assert started.wait(timeout=5), "the transfer never started"
        with pytest.raises(Exception, match="transfer"):
            s.serial_reader_start("loop")
        release.set()
        th.join(timeout=10)
        assert "error" not in result, result.get("error")
    finally:
        s.close()
