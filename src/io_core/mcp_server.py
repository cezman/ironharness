"""ironharness MCP server: io-core tools for agents.

MCP-first convention: agents do I/O only through this server, and every
session operation is journaled automatically.

Configuration via environment:
    IRONHARNESS_HOME     — base for the journal and sandbox (default ~/.ironharness)
    IRONHARNESS_SANDBOX  — file sandbox root (default IRONHARNESS_HOME/sandbox)
    IRONHARNESS_ALLOW_REAL_FLASH — set to 1 to allow live esp_flash/esp_erase
    IRONHARNESS_ALLOWED_HOSTS — comma-separated host / host:port allowlist for
                         modbus/mqtt (unset = allow all; denials -> PolicyViolation)
    IRONHARNESS_ENABLED_KINDS — comma list of serial,modbus,mqtt,esp,file to enable
                         (unset = all enabled)
    IRONHARNESS_MAX_CONNECTIONS — ceiling on simultaneously open transports per
                         session (unset = unlimited; denial = PolicyViolation,
                         journaled)
    IRONHARNESS_TRANSPORT_DEADLINE — per-transport operation deadline in seconds
                         since open (default 600; 0/off disables). A long-lived
                         session hitting it gets OperationTimeout on the next
                         operation - re-open the transport.
    IRONHARNESS_TRANSPORT_RATE — max_calls/window_seconds (e.g. "100/60");
                         unset = no rate limit. Applies to serial/modbus/mqtt
                         operations through the session.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from mcp.server import MCPServer

from io_core.session import Session

mcp = MCPServer("ironharness")

_session: Session | None = None
_session_lock = threading.Lock()


def get_session() -> Session:
    global _session
    # double-checked locking: MCP may dispatch tool calls concurrently, and two
    # racing first calls used to build two Sessions (one journal handle leaked,
    # events split across two files)
    if _session is None:
        with _session_lock:
            if _session is None:
                home = Path(os.environ.get("IRONHARNESS_HOME", Path.home() / ".ironharness"))
                _session = Session(
                    journal_path=home / "journal.jsonl",
                    sandbox_root=os.environ.get("IRONHARNESS_SANDBOX", home / "sandbox"),
                    actor="mcp",
                )
    return _session


def reset_session() -> None:
    """Resets the session (for tests and environment switches)."""
    global _session
    with _session_lock:
        if _session is not None:
            _session.close()
        _session = None


@mcp.tool()
def echo(text: str) -> str:
    """Returns the text back. Service tool to verify the MCP setup works."""
    return text


# --- serial (binary data as hex strings, JSON-friendly) ---


@mcp.tool()
def serial_open(name: str, port: str, baudrate: int = 115200, timeout: float = 1.0) -> str:
    """Opens a named serial port (COM3, /dev/ttyUSB0, loop:// for tests)."""
    get_session().serial_open(name, port, baudrate=baudrate, timeout=timeout)
    return f"ok: serial {name!r} -> {port}"


@mcp.tool()
def serial_write(name: str, data_hex: str) -> int:
    """Writes bytes to the serial port; data_hex is a hex string (e.g. 48656c6c6f)."""
    return get_session().serial_write(name, data_hex)


@mcp.tool()
def serial_read(name: str, size: int = 64) -> str:
    """Reads up to size bytes from the serial port; returns a hex string ("" — timeout)."""
    return get_session().serial_read(name, size)


@mcp.tool()
def serial_read_line(name: str, max_len: int = 256) -> str:
    """Reads one \\n-terminated line; returns a hex string."""
    return get_session().serial_read_line(name, max_len)


@mcp.tool()
def serial_close(name: str) -> str:
    """Closes the named serial port and releases it for other applications."""
    get_session().serial_close(name)
    return f"ok: serial {name!r} closed"


# --- modbus ---


@mcp.tool()
def modbus_open(name: str, host: str, port: int = 502, device_id: int = 1) -> str:
    """Opens a named Modbus TCP connection."""
    get_session().modbus_open(name, host, port=port, device_id=device_id)
    return f"ok: modbus {name!r} -> {host}:{port}"


@mcp.tool()
def modbus_read(name: str, address: int, count: int = 1) -> list[int]:
    """Reads count holding registers starting at address."""
    return get_session().modbus_read(name, address, count)


@mcp.tool()
def modbus_write(name: str, address: int, values: list[int]) -> str:
    """Writes holding registers: one value -> FC6, several -> FC16."""
    get_session().modbus_write(name, address, values)
    return f"ok: wrote {len(values)} register(s) at address {address}"


@mcp.tool()
def modbus_close(name: str) -> str:
    """Closes the named Modbus TCP connection."""
    get_session().modbus_close(name)
    return f"ok: modbus {name!r} closed"


# --- mqtt ---


@mcp.tool()
def mqtt_open(name: str, host: str, port: int = 1883, client_id: str = "", timeout: float = 3.0) -> str:
    """Opens a named MQTT connection to a broker. Plaintext TCP (no TLS/auth) — for bench use."""
    get_session().mqtt_open(name, host, port=port, client_id=client_id, timeout=timeout)
    return f"ok: mqtt {name!r} -> {host}:{port}"


@mcp.tool()
def mqtt_publish(name: str, topic: str, payload: str, qos: int = 0, retain: bool = False) -> str:
    """Publishes a message to a topic (payload is text)."""
    get_session().mqtt_publish(name, topic, payload, qos=qos, retain=retain)
    return f"ok: published to {topic!r}"


@mcp.tool()
def mqtt_subscribe(name: str, topic: str, qos: int = 0) -> str:
    """Subscribes to a topic (wildcards allowed: sensors/#, +/temperature)."""
    get_session().mqtt_subscribe(name, topic, qos=qos)
    return f"ok: subscribed to {topic!r}"


@mcp.tool()
def mqtt_read(name: str, timeout: float = 1.0) -> dict[str, str] | None:
    """Reads the next incoming message {"topic", "payload"}; null — timeout."""
    return get_session().mqtt_read(name, timeout)


@mcp.tool()
def mqtt_close(name: str) -> str:
    """Closes the named MQTT connection and stops its network loop."""
    get_session().mqtt_close(name)
    return f"ok: mqtt {name!r} closed"


# --- esp (flashing; needs the [flash] extra and IRONHARNESS_ALLOW_REAL_FLASH=1) ---


@mcp.tool()
def esp_image_info(firmware_path: str, chip: str = "esp32") -> dict:
    """Parses a .bin firmware image without hardware: entrypoint, segments, flash params."""
    return get_session().esp_image_info(firmware_path)


@mcp.tool()
def esp_flash(port: str, firmware_path: str, addr: int = 0x1000, baud: int = 921600) -> str:
    """Flashes an image to the board on the given port (classic ESP32: addr=0x1000).

    Requires the [flash] extra and IRONHARNESS_ALLOW_REAL_FLASH=1 (modifies real hardware).
    """
    return get_session().esp_flash(port, firmware_path, addr=addr, baud=baud)


@mcp.tool()
def esp_erase(port: str, baud: int = 921600) -> str:
    """Erases the board's entire flash (irreversible).

    Requires the [flash] extra and IRONHARNESS_ALLOW_REAL_FLASH=1 (modifies real hardware).
    """
    return get_session().esp_erase(port, baud=baud)


# --- files (sandboxed) ---


@mcp.tool()
def file_write(path: str, content: str) -> int:
    """Writes a text file (utf-8) inside the sandbox; escaping the sandbox is denied."""
    return get_session().file_write(path, content)


@mcp.tool()
def file_read(path: str) -> str:
    """Reads a text file from the sandbox."""
    return get_session().file_read(path)


@mcp.tool()
def file_list(path: str = ".") -> list[str]:
    """Lists files and directories in the sandbox (relative paths)."""
    return get_session().file_list(path)


@mcp.tool()
def file_delete(path: str) -> str:
    """Deletes a file from the sandbox."""
    get_session().file_delete(path)
    return f"ok: deleted {path}"


def main() -> None:
    """Entry point of the ironharness-mcp console script (and python -m)."""
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
