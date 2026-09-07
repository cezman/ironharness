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
    assert {"echo", "serial_open", "serial_write", "serial_read", "modbus_open",
            "modbus_read", "modbus_write", "mqtt_open", "mqtt_publish", "mqtt_subscribe",
            "mqtt_read", "esp_image_info", "esp_flash", "esp_erase",
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
