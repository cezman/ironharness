"""Тесты MCP-сервера: инструменты регистрируются и работают через Session.

Инструменты вызываем напрямую (декоратор tool() возвращает исходную функцию)
— транспорт stdio проверяется вручную после перезапуска сессии агента.
"""

import asyncio
import json
from pathlib import Path

import pytest

from io_core import ModbusSimServer, SandboxViolation
from io_core.mcp_server import (
    echo,
    esp_image_info,
    file_delete,
    file_list,
    file_read,
    file_write,
    mcp,
    modbus_open,
    modbus_read,
    modbus_write,
    mqtt_open,
    mqtt_publish,
    mqtt_read,
    mqtt_subscribe,
    reset_session,
    serial_close,
    serial_open,
    serial_read,
    serial_write,
)

FIRMWARE = Path(__file__).parents[1] / "src/ironbench/tasks/_firmware/ESP32_GENERIC-20251209-v1.27.0.bin"


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONHARNESS_HOME", str(tmp_path / "home"))
    reset_session()
    yield tmp_path / "home"
    reset_session()


def test_tools_are_registered():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert {"echo", "serial_open", "serial_write", "serial_read", "serial_close",
            "modbus_open", "modbus_read", "modbus_write", "modbus_close",
            "mqtt_open", "mqtt_publish", "mqtt_subscribe", "mqtt_read", "mqtt_close",
            "esp_image_info", "esp_flash", "esp_erase",
            "file_write", "file_read", "file_list"} <= names


def test_echo(mcp_env):
    assert echo("ping") == "ping"


def test_file_tools_roundtrip(mcp_env):
    assert file_write("sub/f.txt", "hello") > 0
    assert file_read("sub/f.txt") == "hello"
    assert file_list() == ["sub", "sub/f.txt"]
    file_delete("sub/f.txt")
    assert file_list() == ["sub"]


def test_file_escape_blocked(mcp_env):
    with pytest.raises(SandboxViolation):
        file_write("../evil.txt", "no")


def test_serial_tools_roundtrip(mcp_env):
    serial_open("s", "loop://", timeout=0.5)
    serial_write("s", "deadbeef")
    assert serial_read("s", 4) == "deadbeef"
    assert serial_close("s") == "ok: serial 's' closed"
    with pytest.raises(KeyError):
        serial_write("s", "00")


def test_modbus_tools_roundtrip(mcp_env):
    with ModbusSimServer(port=0, registers=[1, 2] + [0] * 62) as srv:
        modbus_open("m", "127.0.0.1", port=srv.port)
        modbus_write("m", 0, [1, 2])
        assert modbus_read("m", 0, 2) == [1, 2]


class _FakeMqttTransport:
    """Офлайн-заглушка MqttTransport: брокера нет, одно заранее положенное сообщение."""

    def __init__(self, host, *, port=1883, client_id="", timeout=3.0, on_event=None):
        self.inbox = [{"topic": "cmd/led", "payload": "done"}]

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def publish(self, topic, payload, *, qos=0, retain=False):
        pass

    def subscribe(self, topic, *, qos=0):
        pass

    def read_message(self, timeout: float = 1.0):
        return self.inbox.pop(0) if self.inbox else None


def test_mqtt_tools_roundtrip(mcp_env, monkeypatch):
    monkeypatch.setattr("io_core.session.MqttTransport", _FakeMqttTransport)
    assert "ok" in mqtt_open("bus", "broker.test")
    assert "ok" in mqtt_subscribe("bus", "cmd/#")
    assert "ok" in mqtt_publish("bus", "cmd/led", "on")
    assert mqtt_read("bus") == {"topic": "cmd/led", "payload": "done"}
    assert mqtt_read("bus") is None  # буфер пуст → таймаут


def test_journal_lands_in_home(mcp_env):
    file_write("f.txt", "data")
    journal = json.loads((mcp_env / "journal.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert journal["actor"] == "mcp"
    assert journal["kind"] == "file_write"


def test_esp_image_info_offline(mcp_env):
    """Разбор реального образа через MCP без железа."""
    info = esp_image_info(str(FIRMWARE))
    assert info["chip"] == "esp32"
    assert len(info["segments"]) >= 3


def test_esp_image_info_passes_chip_to_session(mcp_env, monkeypatch):
    # IH-29: аргумент chip раньше принимался и молча отбрасывался - агент
    # получал разбор под esp32, запросив другой чип
    from io_core import Session

    captured = {}

    def fake_info(self, firmware_path, chip="esp32"):
        captured["chip"] = chip
        return {"chip": chip}

    monkeypatch.setattr(Session, "esp_image_info", fake_info)
    assert esp_image_info("fw.bin", chip="esp32s3") == {"chip": "esp32s3"}
    assert captured["chip"] == "esp32s3"


def test_get_session_race_creates_one_session(monkeypatch, tmp_path):
    # IH-12: два конкурентных первых вызова get_session() создают ровно одну
    # Session (раньше обе проходили проверку на None и одна Session с журналом
    # терялась — файл оставался открытым, события расходились по двум файлам).
    import threading
    import time as time_module

    import io_core.mcp_server as mcp_module

    real_session = mcp_module.Session

    class SlowSession(real_session):
        def __init__(self, *args, **kwargs):
            time_module.sleep(0.2)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mcp_module, "Session", SlowSession)
    monkeypatch.setenv("IRONHARNESS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("IRONHARNESS_SANDBOX", str(tmp_path / "sandbox"))
    mcp_module.reset_session()
    try:
        results: list = []

        def worker():
            results.append(mcp_module.get_session())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 2 and results[0] is results[1]
    finally:
        mcp_module.reset_session()
