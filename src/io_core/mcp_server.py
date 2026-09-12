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
    IRONHARNESS_TRANSPORT_DEADLINE — per-transport idle deadline in seconds
                         (default 600; 0/off disables). Every successful
                         operation pushes the deadline out (sliding window,
                         IH-35), so long interactive sessions survive; a
                         connection with no successful operation for the
                         window gets OperationTimeout with a reopen hint -
                         close and reopen the connection.
    IRONHARNESS_TRANSPORT_RATE — max_calls/window_seconds (e.g. "100/60");
                         unset = no rate limit. Applies to serial/modbus/mqtt
                         operations through the session.

Stdout hygiene (IH-20): over stdio the wire must carry only JSON-RPC. The
SDK's stdio_server diverts fd 1 to stderr while serving (the protocol is
written to a private duplicate of the real stdout), so on the normal serving
path stray prints and library logging do not reach the wire (the claim is
best-effort: a non-fd-backed stdout serves in place without diversion); the
SDK's own logging is wired to stderr. Regression tests in
tests/test_mcp_stdio.py pin this end-to-end: every byte the client reads
from the child's stdout must parse as a JSON-RPC message, and a deliberately
noisy handler must land on stderr.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def echo(text: str) -> str:
    """Returns the text back. Service tool to verify the MCP setup works."""
    return text


# Tool annotation conventions (IH-20), applied explicitly to every tool:
# - readOnlyHint=True only where nothing is consumed or mutated: echo,
#   serial_list (host device table), file_read/file_list (sandbox),
#   esp_image_info (parses a file), and modbus_read (an FC3 query - the
#   device answers, its state is untouched). serial_read/serial_read_line/
#   mqtt_read are deliberately NOT readOnly: they drain a stream/queue -
#   a replay loses data for later reads.
# - destructiveHint=True for esp_flash/esp_erase (real hardware),
#   file_delete, and file_write (an overwrite is not an additive update);
#   plain state writes stay destructiveHint=False.
# - openWorldHint=True on the tools that reach outside the process
#   (connects, I/O over ports/hosts/brokers); the close/subscribe tools
#   leave it unset (the spec default is true anyway).
# - idempotentHint is left unset everywhere on purpose: nothing here is
#   state-idempotent (a repeated *_open on the same name is an error, not a
#   no-op - Session refuses "already open"); the read-only tools re-execute
#   on every call rather than being cached no-ops.


# --- serial (binary data as hex strings, JSON-friendly) ---


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
def serial_list() -> list[dict]:
    """Lists host serial ports with USB identity: device, vid, pid, description.
    Board COM numbers float across re-plugs - enumerate before serial_open.
    Nothing is opened or consumed."""
    return get_session().serial_list()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def serial_open(name: str, port: str, baudrate: int = 115200, timeout: float = 1.0) -> str:
    """Opens a named serial port (COM3, /dev/ttyUSB0, loop:// for tests)."""
    get_session().serial_open(name, port, baudrate=baudrate, timeout=timeout)
    return f"ok: serial {name!r} -> {port}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def serial_write(name: str, data_hex: str) -> int:
    """Writes bytes to the serial port; data_hex is a hex string (e.g. 48656c6c6f)."""
    return get_session().serial_write(name, data_hex)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def serial_read(name: str, size: int = 64) -> str:
    """Reads up to size bytes from the serial port; returns a hex string ("" — timeout)."""
    return get_session().serial_read(name, size)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def serial_read_line(name: str, max_len: int = 256) -> str:
    """Reads one \\n-terminated line; returns a hex string."""
    return get_session().serial_read_line(name, max_len)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def serial_close(name: str) -> str:
    """Closes the named serial port and releases it for other applications."""
    get_session().serial_close(name)
    return f"ok: serial {name!r} closed"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def serial_reset(name: str, pulse_sec: float = 0.1, settle_sec: float = 2.0) -> str:
    """Resets the board with an RTS pulse (ESP32: RTS->EN, DTR low = normal
    boot). Clean state between solve attempts or after a hung REPL; open()
    never resets - reset happens only here. Waits settle_sec for the boot."""
    get_session().serial_reset(name, pulse_sec=pulse_sec, settle_sec=settle_sec)
    return f"ok: serial {name!r} reset (pulse {pulse_sec}s, settle {settle_sec}s)"


# --- serial background reader (IH-18): tail + expect-style waits ---


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def serial_reader_start(name: str, max_bytes: int = 65536) -> dict:
    """Starts a background reader on an open serial port: keeps draining the
    device into a bounded buffer (drop-oldest) so output printed between tool
    calls is not lost. While it runs, serial_read/serial_read_line are refused
    - use serial_tail/serial_read_until."""
    return get_session().serial_reader_start(name, max_bytes=max_bytes)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def serial_reader_stop(name: str) -> dict:
    """Stops the background reader of the named serial port (returns final buffer stats)."""
    return get_session().serial_reader_stop(name)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
def serial_tail(name: str, size: int = 4096) -> dict:
    """The newest data received by the background reader (non-destructive):
    {"data_hex", "text", "dropped", "alive", "error"} - dropped > 0 means
    older bytes were evicted by the buffer bound or consumed by
    serial_read_until; alive=false means the reader thread died (port
    failure - not the same as a silent device)."""
    return get_session().serial_tail(name, size)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def serial_read_until(name: str, pattern: str, timeout: float = 10.0) -> dict:
    """Waits until the pattern (utf-8 text) appears in fresh reader data and
    consumes the buffer up to the end of the match: {"found", "data_hex",
    "text", "alive", "error"}. found=false on timeout (text = whatever is
    unconsumed)."""
    return get_session().serial_read_until(name, pattern, timeout)


# --- modbus ---


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def modbus_open(name: str, host: str, port: int = 502, device_id: int = 1) -> str:
    """Opens a named Modbus TCP connection."""
    get_session().modbus_open(name, host, port=port, device_id=device_id)
    return f"ok: modbus {name!r} -> {host}:{port}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
def modbus_read(name: str, address: int, count: int = 1) -> list[int]:
    """Reads count holding registers starting at address."""
    return get_session().modbus_read(name, address, count)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def modbus_write(name: str, address: int, values: list[int]) -> str:
    """Writes holding registers: one value -> FC6, several -> FC16."""
    get_session().modbus_write(name, address, values)
    return f"ok: wrote {len(values)} register(s) at address {address}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def modbus_close(name: str) -> str:
    """Closes the named Modbus TCP connection."""
    get_session().modbus_close(name)
    return f"ok: modbus {name!r} closed"


# --- mqtt ---


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def mqtt_open(name: str, host: str, port: int = 1883, client_id: str = "", timeout: float = 3.0) -> str:
    """Opens a named MQTT connection to a broker. Plaintext TCP (no TLS/auth) — for bench use."""
    get_session().mqtt_open(name, host, port=port, client_id=client_id, timeout=timeout)
    return f"ok: mqtt {name!r} -> {host}:{port}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def mqtt_publish(name: str, topic: str, payload: str, qos: int = 0, retain: bool = False) -> str:
    """Publishes a message to a topic (payload is text)."""
    get_session().mqtt_publish(name, topic, payload, qos=qos, retain=retain)
    return f"ok: published to {topic!r}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def mqtt_subscribe(name: str, topic: str, qos: int = 0) -> str:
    """Subscribes to a topic (wildcards allowed: sensors/#, +/temperature)."""
    get_session().mqtt_subscribe(name, topic, qos=qos)
    return f"ok: subscribed to {topic!r}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def mqtt_read(name: str, timeout: float = 1.0) -> dict[str, str] | None:
    """Reads the next incoming message {"topic", "payload"}; null — timeout."""
    return get_session().mqtt_read(name, timeout)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
def mqtt_close(name: str) -> str:
    """Closes the named MQTT connection and stops its network loop."""
    get_session().mqtt_close(name)
    return f"ok: mqtt {name!r} closed"


# --- esp (flashing; needs the [flash] extra and IRONHARNESS_ALLOW_REAL_FLASH=1) ---


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def esp_image_info(firmware_path: str, chip: str = "esp32") -> dict:
    """Parses a .bin firmware image without hardware: entrypoint, segments, flash params."""
    return get_session().esp_image_info(firmware_path, chip=chip)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
def esp_flash(port: str, firmware_path: str, addr: int = 0x1000, baud: int = 921600) -> str:
    """Flashes an image to the board on the given port (classic ESP32: addr=0x1000).

    Requires the [flash] extra and IRONHARNESS_ALLOW_REAL_FLASH=1 (modifies real hardware).
    """
    return get_session().esp_flash(port, firmware_path, addr=addr, baud=baud)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
def esp_erase(port: str, baud: int = 921600) -> str:
    """Erases the board's entire flash (irreversible).

    Requires the [flash] extra and IRONHARNESS_ALLOW_REAL_FLASH=1 (modifies real hardware).
    """
    return get_session().esp_erase(port, baud=baud)


# --- files (sandboxed) ---


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
def file_write(path: str, content: str) -> int:
    """Writes a text file (utf-8) inside the sandbox; escaping the sandbox is denied."""
    return get_session().file_write(path, content)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def file_read(path: str) -> str:
    """Reads a text file from the sandbox."""
    return get_session().file_read(path)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def file_list(path: str = ".") -> list[str]:
    """Lists files and directories in the sandbox (relative paths)."""
    return get_session().file_list(path)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
def file_delete(path: str) -> str:
    """Deletes a file from the sandbox."""
    get_session().file_delete(path)
    return f"ok: deleted {path}"


def main() -> None:
    """Entry point of the ironharness-mcp console script (and python -m)."""
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
