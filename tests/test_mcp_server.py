"""Тесты MCP-сервера: инструменты регистрируются и работают через Session.

Инструменты вызываем напрямую; декоратор tool() возвращает обёртку,
переводящую доменные ошибки в ToolError (IH-76) — поэтому ожидаем ToolError
с доменной причиной. Транспорт stdio проверяется в test_mcp_stdio.py (IH-30).
"""

import asyncio
import json
from pathlib import Path

import pytest

from io_core import ModbusSimServer, SandboxViolation
from io_core.mcp_server import (
    echo,
    esp_erase,
    esp_flash,
    esp_image_info,
    file_delete,
    file_list,
    file_read,
    file_write,
    get_session,
    mcp,
    modbus_close,
    modbus_open,
    modbus_read,
    modbus_write,
    mqtt_close,
    mqtt_open,
    mqtt_publish,
    mqtt_read,
    mqtt_subscribe,
    reset_session,
    serial_close,
    serial_get,
    serial_list,
    serial_open,
    serial_put,
    serial_read,
    serial_read_line,
    serial_read_until,
    serial_reader_start,
    serial_reader_stop,
    serial_reset,
    serial_tail,
    serial_wait,
    serial_write,
    session_status,
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
            "session_status", "serial_monitor",
            "modbus_open", "modbus_read", "modbus_write", "modbus_close",
            "mqtt_open", "mqtt_publish", "mqtt_subscribe", "mqtt_read", "mqtt_close",
            "esp_image_info", "esp_flash", "esp_erase",
            "file_write", "file_read", "file_list"} <= names


def test_echo(mcp_env):
    assert echo("ping") == "ping"


def test_serial_monitor_tool(mcp_env):
    # tool-level pass: the session method behind the MCP surface (loop://)
    from io_core.mcp_server import get_session, serial_monitor

    get_session().serial_open("c", "loop://", timeout=0.1)
    get_session()._serial_base["c"].write(b"BOOT OK\n")
    r = serial_monitor("c", max_seconds=5, quiet_seconds=0.2, dump_path="cap.bin")
    assert r["stop_reason"] == "quiet"
    assert "BOOT OK" in r["text"]
    assert r["dump"] == "cap.bin"


def test_file_tools_roundtrip(mcp_env):
    assert file_write("sub/f.txt", "hello") > 0
    assert file_read("sub/f.txt") == "hello"
    assert file_list() == ["sub", "sub/f.txt"]
    file_delete("sub/f.txt")
    assert file_list() == ["sub"]


def test_file_escape_blocked(mcp_env):
    # IH-76: tool functions surface domain errors as ToolError with the
    # domain error as __cause__ (the text is what the agent reads)
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as exc_info:
        file_write("../evil.txt", "no")
    assert isinstance(exc_info.value.__cause__, SandboxViolation)


def test_serial_tools_roundtrip(mcp_env):
    from mcp.server.mcpserver.exceptions import ToolError

    serial_open("s", "loop://", timeout=0.5)
    serial_write("s", "deadbeef")
    assert serial_read("s", 4) == "deadbeef"
    assert serial_close("s") == "ok: serial 's' closed"
    with pytest.raises(ToolError) as exc_info:
        serial_write("s", "00")
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_serial_write_io_error_maps_to_tool_error(mcp_env, monkeypatch):
    # IH-76 review: TransportIoError (port gone mid-write) is an operational
    # event with a hint text - mapped, not a crash
    from mcp.server.mcpserver.exceptions import ToolError

    from io_core.errors import TransportIoError

    serial_open("s", "loop://", timeout=0.5)

    def boom(*args, **kwargs):
        raise TransportIoError("port went away mid-write")

    monkeypatch.setattr("io_core.session.Session.serial_write", boom)
    with pytest.raises(ToolError) as exc_info:
        serial_write("s", "deadbeef")
    assert isinstance(exc_info.value.__cause__, TransportIoError)
    assert "port went away" in str(exc_info.value)


def test_domain_errors_wrapper_passes_unmapped_through():
    # IH-76 review, adjusted by audit D5: RuntimeError is now a MAPPED hint
    # carrier (refusal texts, MpReplError) - the dichotomy example moves to a
    # genuinely unmapped exception: an unmapped error is a bug and keeps the
    # SDK crash path (no ToolError conversion)
    from io_core.mcp_server import _domain_errors

    @_domain_errors
    def boom():
        raise TypeError("a bug, not a hint")

    with pytest.raises(TypeError):
        boom()


def test_domain_errors_wrapper_supports_async():
    # IH-76 review: the async branch is a guard for future async tools
    from mcp.server.mcpserver.exceptions import ToolError

    from io_core.mcp_server import _domain_errors

    @_domain_errors
    async def aboom():
        raise ValueError("validation hint")

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(aboom())
    assert "ValueError" in str(exc_info.value)


def test_modbus_tools_roundtrip(mcp_env):
    from mcp.server.mcpserver.exceptions import ToolError

    with ModbusSimServer(port=0, registers=[1, 2] + [0] * 62) as srv:
        modbus_open("m", "127.0.0.1", port=srv.port)
        modbus_write("m", 0, [1, 2])
        assert modbus_read("m", 0, 2) == [1, 2]
        assert "closed" in modbus_close("m")
        # the close is real: the connection is gone from the session
        with pytest.raises(ToolError) as exc_info:
            modbus_read("m", 0, 2)
        assert isinstance(exc_info.value.__cause__, KeyError)


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
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr("io_core.session.MqttTransport", _FakeMqttTransport)
    assert "ok" in mqtt_open("bus", "broker.test")
    assert "ok" in mqtt_subscribe("bus", "cmd/#")
    assert "ok" in mqtt_publish("bus", "cmd/led", "on")
    assert mqtt_read("bus") == {"topic": "cmd/led", "payload": "done"}
    assert mqtt_read("bus") is None  # буфер пуст → таймаут
    assert "ok" in mqtt_close("bus")
    # the close is real: the connection is gone from the session
    with pytest.raises(ToolError) as exc_info:
        mqtt_publish("bus", "cmd/led", "on")
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_journal_lands_in_home(mcp_env):
    file_write("f.txt", "data")
    journal = json.loads((mcp_env / "journal.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert journal["actor"] == "mcp"
    assert journal["kind"] == "file_write"


def test_esp_image_info_offline(mcp_env, monkeypatch):
    """Разбор реального образа через MCP без железа."""
    # esptool is the optional [flash] extra: the CI min-deps job (IH-41)
    # installs the runtime floors only, so this must skip, not fail
    pytest.importorskip("esptool", reason="esp tests need the [flash] extra (esptool)")
    # the shared bin lives outside the sandbox (tasks/_firmware) - parsing it
    # is the documented operator opt-in (IH-77: the sandbox gate is default)
    monkeypatch.setenv("IRONHARNESS_ALLOW_REAL_FLASH", "1")
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
        # IH-124: the env-picked sandbox LOCATION is pinned, not just set -
        # a lost IRONHARNESS_SANDBOX on retransmission would silently fall
        # back to the ~/.ironharness default
        assert results[0].sandbox.root == (tmp_path / "sandbox").resolve()
    finally:
        mcp_module.reset_session()


def test_serial_put_board_error_maps_to_tool_error(mcp_env, monkeypatch):
    # audit D5: MpReplError (the board's own traceback from raw-REPL put/get)
    # and RuntimeError refusal hints must reach the agent as ToolError text -
    # not as an unmapped crash
    from mcp.server.mcpserver.exceptions import ToolError

    from io_core.mprepl import MpReplError

    serial_open("s", "loop://", timeout=0.5)

    def boom(*args, **kwargs):
        raise MpReplError(
            'Traceback (most recent call last):\r\n  File "<stdin>" line 1\r\n'
            "ValueError: buffer too small"
        )

    monkeypatch.setattr("io_core.session.Session.serial_put", boom)
    with pytest.raises(ToolError) as exc_info:
        serial_put("s", "local.py", "main.py")
    assert isinstance(exc_info.value.__cause__, MpReplError)
    assert "buffer too small" in str(exc_info.value)


def test_runtime_error_refusal_maps_to_tool_error(mcp_env, monkeypatch):
    # the RuntimeError family carries tool-refusal hints ("transfer in
    # progress", "use serial_tail") - audit D5: those texts reach the agent
    from mcp.server.mcpserver.exceptions import ToolError

    serial_open("s", "loop://", timeout=0.5)

    def busy(*args, **kwargs):
        raise RuntimeError("transfer in progress - call session_status first")

    monkeypatch.setattr("io_core.session.Session.serial_put", busy)
    with pytest.raises(ToolError) as exc_info:
        serial_put("s", "local.py", "main.py")
    assert "transfer in progress" in str(exc_info.value)


# --- tool-body coverage for the older tools (IH-123): appearing in
# tools/list is not coverage - every tool needs at least one executed body
# call, success paths included where a fake can stand in for the device ---


class _FakeComPort:
    """A pyserial comports() entry with USB identity."""

    def __init__(self, device, vid, pid):
        self.device, self.vid, self.pid = device, vid, pid
        self.serial_number, self.location, self.description = "SN1", "1-4", "USB-SERIAL"


def test_serial_list_tool_enumerates_fake_ports(mcp_env, monkeypatch):
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [_FakeComPort("COM6", 0x1A86, 0x7523)],
    )
    assert serial_list() == [
        {"device": "COM6", "vid": "1a86", "pid": "7523",
         "serial_number": "SN1", "location": "1-4", "description": "USB-SERIAL"}
    ]


def test_session_status_tool_reports_state(mcp_env):
    serial_open("s", "loop://", timeout=0.5)
    st = session_status()
    assert st["transports"]["s"]["kind"] == "serial"
    assert st["readers"] == {} and st["transfers"] == [] and st["monitors"] == []
    assert st["sandbox"]


def test_serial_wait_tool_finds_the_matching_port(mcp_env, monkeypatch):
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [_FakeComPort("COM6", 0x1A86, 0x7523)],
    )
    assert serial_wait("1A86", "7523", timeout=2.0)["device"] == "COM6"


def test_serial_read_line_tool_roundtrip(mcp_env):
    serial_open("s", "loop://", timeout=0.5)
    serial_write("s", "deadbeef0a")
    assert serial_read_line("s") == "deadbeef0a"


def test_serial_reset_tool_runs_on_loop_port(mcp_env):
    # loop:// takes the line assignments as no-ops (documented): the tool
    # body, the validation and the journaling still run end to end
    from mcp.server.mcpserver.exceptions import ToolError

    serial_open("s", "loop://", timeout=0.5)
    assert "reset" in serial_reset("s", pulse_sec=0, settle_sec=0)
    # the tool forwards the session call: an unknown connection must fail
    with pytest.raises(ToolError) as exc_info:
        serial_reset("nope", pulse_sec=0, settle_sec=0)
    assert isinstance(exc_info.value.__cause__, KeyError)


def test_serial_get_tool_pulls_a_board_file_into_the_sandbox(mcp_env):
    from test_mprepl import FakeRawBoard

    board = FakeRawBoard()
    board.files["/main.py"] = b"print('firmware')\n"
    s = get_session()
    s._transports["b"] = board
    s._kinds["b"] = "serial"
    assert "get 18 bytes" in serial_get("b", "/main.py", "pulled/main.py")
    assert file_read("pulled/main.py") == "print('firmware')\n"


def test_serial_reader_tools_body_roundtrip(mcp_env):
    serial_open("s", "loop://", timeout=0.5)
    started = serial_reader_start("s")
    assert started == {"buffered": 0, "dropped": 0, "chunks": 0}
    tail = serial_tail("s")
    assert tail["data_hex"] == "" and tail["alive"] is True and tail["error"] is None
    serial_write("s", "6f6b")  # loop:// echoes it back into the reader
    hit = serial_read_until("s", "ok", timeout=5.0)
    assert hit["found"] is True and hit["text"] == "ok"
    stopped = serial_reader_stop("s")
    assert stopped["stopped"] is True and stopped["buffered"] == 0


def test_esp_flash_and_erase_tools_body(mcp_env, monkeypatch):
    """Success-path body calls against the esptool fakes (no board)."""
    pytest.importorskip("esptool", reason="esp tests need the [flash] extra (esptool)")
    from test_esp import FakeEsptool

    fake = FakeEsptool()
    fake.install(monkeypatch)  # also sets IRONHARNESS_ALLOW_REAL_FLASH=1
    assert "ok" in esp_flash("loop://", str(FIRMWARE))
    assert "ok" in esp_erase("loop://")
    assert fake.steps() == [
        "connect", "run_stub", "attach_flash", "write_flash",
        "connect", "run_stub", "attach_flash", "erase_flash",
    ]
