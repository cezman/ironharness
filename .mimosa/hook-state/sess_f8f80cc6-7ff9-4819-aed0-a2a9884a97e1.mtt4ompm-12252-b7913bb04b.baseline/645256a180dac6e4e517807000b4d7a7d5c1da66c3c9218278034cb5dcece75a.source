"""ESP flashing tools tests: image parsing runs offline on a real .bin,
flash/erase run against esptool fakes (no board attached).

The fakes replay the canonical esptool CLI sequence:
connect_esp (context manager) -> run_stub -> change_baud -> attach_flash -> operation.
"""

import json
from pathlib import Path
from typing import Self

import pytest

pytest.importorskip("esptool", reason="esp tests need the [flash] extra (esptool)")

from esptool.cmds import FatalError

import io_core.esp_flash as esp_mod
from io_core import EspFlasher, JsonlJournal, Session, read_events

FIRMWARE = Path(__file__).parents[1] / "src/ironbench/tasks/_firmware/ESP32_GENERIC-20251209-v1.27.0.bin"


class FakePort:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeEsp:
    """Мимикрия под ESPLoader: контекст-менеджер, закрывающий порт."""

    def __init__(self) -> None:
        self._port = FakePort()
        self.bauds: list[int] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._port.close()

    def change_baud(self, baud: int) -> None:
        self.bauds.append(baud)


class FakeEsptool:
    """Подменяет модульные функции esptool и записывает вызовы: (имя, payload)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.esp = FakeEsp()
        self.fail_at: str | None = None  # имя шага, на котором бросить ошибку

    def install(self, monkeypatch) -> None:
        # fakes replace the real esptool path, but the live-flash opt-in guard still applies
        monkeypatch.setenv("IRONHARNESS_ALLOW_REAL_FLASH", "1")
        monkeypatch.setattr(esp_mod, "connect_esp", self._connect)
        monkeypatch.setattr(esp_mod, "run_stub", self._step("run_stub"))
        monkeypatch.setattr(esp_mod, "attach_flash", self._step("attach_flash"))
        monkeypatch.setattr(
            esp_mod,
            "write_flash",
            lambda esp, addr_data, **k: self._op("write_flash", addr_data=addr_data),
        )
        monkeypatch.setattr(esp_mod, "erase_flash", lambda esp, **k: self._op("erase_flash"))

    def _connect(self, *, port, chip, **k):
        if self.fail_at == "connect":
            raise FatalError("could not open port")
        self.calls.append(("connect", {"port": port, "chip": chip}))
        return self.esp

    def _step(self, name):
        def step(esp, *a, **k):
            self._op(name)
            return esp  # транспорт делает esp = run_stub(esp)

        return step

    def _op(self, name, **payload):
        if self.fail_at == name:
            raise OSError(f"{name} failed")
        self.calls.append((name, payload))

    def steps(self) -> list[str]:
        return [name for name, _payload in self.calls]


def test_image_info_real_firmware(tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        info = EspFlasher(on_event=jr).image_info(FIRMWARE)
    assert info["chip"] == "esp32"
    assert info["size"] == FIRMWARE.stat().st_size
    assert info["entrypoint"].startswith("0x4")  # IRAM/flash entry ESP32
    assert len(info["segments"]) >= 3
    assert info["flash_mode"] in {"qio", "qout", "dio", "dout"}
    assert info["flash_size"].endswith(("KB", "MB"))
    assert info["flash_freq"] in {"80m", "40m", "26m", "20m"}
    assert [e["kind"] for e in read_events(jpath)] == ["esp_image_info"]


def test_image_info_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        EspFlasher().image_info("nope.bin")


def test_flash_canonical_sequence(tmp_path, monkeypatch):
    fake = FakeEsptool()
    fake.install(monkeypatch)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        result = EspFlasher(on_event=jr).flash("COM7", FIRMWARE)
    assert "flashed" in result
    assert fake.calls[0] == ("connect", {"port": "COM7", "chip": "esp32"})
    assert "run_stub" in fake.steps()
    assert ("write_flash", {"addr_data": [(0x1000, str(FIRMWARE))]}) in fake.calls
    assert fake.esp.bauds == [921600]  # повышение baud только после run_stub
    assert fake.esp._port.closed  # порт закрыт контекст-менеджером
    assert [e["kind"] for e in read_events(jpath)] == ["esp_flash"]


def test_flash_write_failure_journals_and_closes_port(tmp_path, monkeypatch):
    fake = FakeEsptool()
    fake.fail_at = "write_flash"
    fake.install(monkeypatch)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(ConnectionError, match="failed to flash"):
        EspFlasher(on_event=jr).flash("COM7", FIRMWARE)
    assert fake.esp._port.closed  # неудача операции не оставляет порт открытым
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["esp_flash_failed"]
    assert "write_flash failed" in events[0]["error"]


def test_flash_connect_failure(monkeypatch):
    fake = FakeEsptool()
    fake.fail_at = "connect"
    fake.install(monkeypatch)
    with pytest.raises(ConnectionError, match="failed to flash"):
        EspFlasher().flash("COM9", FIRMWARE)


def test_erase_canonical_sequence(tmp_path, monkeypatch):
    fake = FakeEsptool()
    fake.install(monkeypatch)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        result = EspFlasher(on_event=jr).erase("/dev/ttyUSB0")
    assert "erased" in result
    assert fake.steps() == ["connect", "run_stub", "attach_flash", "erase_flash"]
    assert fake.esp._port.closed
    assert [e["kind"] for e in read_events(jpath)] == ["esp_erase"]


def test_erase_connect_failure(tmp_path, monkeypatch):
    fake = FakeEsptool()
    fake.fail_at = "connect"
    fake.install(monkeypatch)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(ConnectionError, match="failed to erase"):
        EspFlasher(on_event=jr).erase("COM9")
    assert [e["kind"] for e in read_events(jpath)] == ["esp_erase_failed"]


def test_flash_missing_firmware_raises(monkeypatch):
    def no_connect(*a, **k):
        raise AssertionError("esptool не должен зваться до проверки образа")

    monkeypatch.setattr(esp_mod, "connect_esp", no_connect)
    with pytest.raises(FileNotFoundError):
        EspFlasher().flash("COM7", "nope.bin")


def test_session_esp_ops(tmp_path, monkeypatch):
    fake = FakeEsptool()
    fake.install(monkeypatch)

    jpath = tmp_path / "session.jsonl"
    s = Session(jpath, tmp_path / "sandbox", actor="test")
    s.esp_image_info(str(FIRMWARE), chip="esp32")
    s.esp_flash("COM7", str(FIRMWARE))
    s.close()
    kinds = [json.loads(line)["kind"] for line in jpath.read_text(encoding="utf-8").splitlines()]
    assert kinds == ["esp_image_info", "esp_flash"]
