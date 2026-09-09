"""Agent session: named transports + sandbox + unified journal.

The unit of agent work: MCP tools operate on a session, and every operation
lands in the session journal automatically (the "no log = didn't happen"
convention).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from io_core.errors import PolicyViolation
from io_core.file_sandbox import FileSandbox
from io_core.journal import JsonlJournal
from io_core.modbus_transport import ModbusTransport
from io_core.mqtt_transport import MqttTransport
from io_core.policy import MAX_CONNECTIONS_ENV, AccessPolicy, parse_max_connections
from io_core.serial_transport import SerialTransport

DEFAULT_BOOTLOADER_OFFSET = 0x1000  # classic ESP32 (canonical value lives in esp_flash)

EventHook = Callable[[str, dict[str, Any]], None]


class Session:
    """Agent session: named transports + sandbox + unified journal.

    Thread safety (IH-12): Session methods may be called from several threads
    (MCP servers dispatch tool calls concurrently; MQTT emits come from the
    paho network thread). The transport registry is guarded by an internal
    lock, and open is check-and-insert under the same lock - a duplicate or
    over-the-limit open can never slip through a check-then-act window.
    """

    def __init__(
        self,
        journal_path: str | Path,
        sandbox_root: str | Path,
        *,
        actor: str = "agent",
    ) -> None:
        self.journal = JsonlJournal(journal_path, actor=actor)
        self.sandbox = FileSandbox(sandbox_root, on_event=self.journal)
        self._transports: dict[str, Any] = {}
        self._lock = threading.RLock()

    def _check_free(self, name: str) -> None:
        if name in self._transports:
            raise KeyError(f"transport {name!r} is already open")

    def _check_connection_limit(self) -> None:
        """Connection-count policy; called under the session lock together with
        the registry insert, so parallel opens cannot slip past the ceiling."""
        limit = parse_max_connections(os.environ.get(MAX_CONNECTIONS_ENV))
        if limit is not None and len(self._transports) >= limit:
            self.journal(
                "policy_violation",
                {"rule": "max_connections", "limit": limit, "open": len(self._transports)},
            )
            raise PolicyViolation(
                f"connection limit reached "
                f"({len(self._transports)}/{limit} via {MAX_CONNECTIONS_ENV})"
            )

    def _check_kind(self, kind: str) -> None:
        AccessPolicy.from_env(on_event=self.journal).check_kind(kind)

    def _get(self, name: str) -> Any:
        with self._lock:
            try:
                return self._transports[name]
            except KeyError:
                raise KeyError(f"transport {name!r} is not open") from None

    def _journal_for(self, name: str) -> EventHook:
        """Per-connection journal hook: stamps every transport event with the
        connection name, so a session with two devices stays attributable -
        without it a read/write event carries only data and the journal cannot
        say whose bytes they are. The transport keeps owning its own fields
        (open/close events already carry port/host); conn is filled first and
        never shadows them."""
        def hook(kind: str, data: dict[str, Any]) -> None:
            self.journal(kind, {"conn": name, **data})

        return hook

    # --- serial ---

    def serial_open(
        self, name: str, port: str, *, baudrate: int = 115200, timeout: float = 1.0
    ) -> None:
        # check + open + insert under one lock: two parallel opens of one name
        # used to both pass the free-check, open two real ports and lose one of
        # them (it stayed open past session.close() - a leaked COM port)
        with self._lock:
            self._check_kind("serial")
            self._check_free(name)
            self._check_connection_limit()
            t = SerialTransport(
                port, baudrate=baudrate, timeout=timeout, on_event=self._journal_for(name)
            )
            t.open()
            self._transports[name] = t

    def serial_write(self, name: str, data_hex: str) -> int:
        return self._get(name).write(bytes.fromhex(data_hex))

    def serial_read(self, name: str, size: int = 64) -> str:
        return self._get(name).read(size).hex()

    def serial_read_line(self, name: str, max_len: int = 256) -> str:
        return self._get(name).read_line(max_len).hex()

    # --- modbus ---

    def modbus_open(
        self,
        name: str,
        host: str,
        *,
        port: int = 502,
        device_id: int = 1,
        timeout: float = 3.0,
    ) -> None:
        with self._lock:
            self._check_kind("modbus")
            self._check_free(name)
            self._check_connection_limit()
            t = ModbusTransport(
                host,
                port=port,
                device_id=device_id,
                timeout=timeout,
                on_event=self._journal_for(name),
            )
            t.open()
            self._transports[name] = t

    def modbus_read(self, name: str, address: int, count: int = 1) -> list[int]:
        return self._get(name).read_holding(address, count)

    def modbus_write(self, name: str, address: int, values: list[int]) -> None:
        t = self._get(name)
        if len(values) == 1:
            t.write_register(address, values[0])
        else:
            t.write_registers(address, values)

    # --- mqtt ---

    def mqtt_open(
        self,
        name: str,
        host: str,
        *,
        port: int = 1883,
        client_id: str = "",
        timeout: float = 3.0,
    ) -> None:
        with self._lock:
            self._check_kind("mqtt")
            self._check_free(name)
            self._check_connection_limit()
            t = MqttTransport(
                host,
                port=port,
                client_id=client_id,
                timeout=timeout,
                on_event=self._journal_for(name),
            )
            t.open()
            self._transports[name] = t

    def mqtt_publish(self, name: str, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> None:
        self._get(name).publish(topic, payload, qos=qos, retain=retain)

    def mqtt_subscribe(self, name: str, topic: str, *, qos: int = 0) -> None:
        self._get(name).subscribe(topic, qos=qos)

    def mqtt_read(self, name: str, timeout: float = 1.0) -> dict[str, str] | None:
        return self._get(name).read_message(timeout)

    # --- esp (flashing via esptool; needs the [flash] extra) ---

    def esp_image_info(self, firmware_path: str, chip: str = "esp32") -> dict[str, Any]:
        self._check_kind("esp")
        from io_core.esp_flash import EspFlasher  # lazy: esptool is an optional dependency

        return EspFlasher(chip=chip, on_event=self.journal).image_info(firmware_path)

    def esp_flash(
        self, port: str, firmware_path: str, *, addr: int = DEFAULT_BOOTLOADER_OFFSET, baud: int = 921600
    ) -> str:
        self._check_kind("esp")
        from io_core.esp_flash import EspFlasher  # lazy: esptool is an optional dependency

        return EspFlasher(on_event=self.journal).flash(port, firmware_path, addr=addr, baud=baud)

    def esp_erase(self, port: str, *, baud: int = 921600) -> str:
        self._check_kind("esp")
        from io_core.esp_flash import EspFlasher  # lazy: esptool is an optional dependency

        return EspFlasher(on_event=self.journal).erase(port, baud=baud)

    # --- файлы (песочница) ---

    def file_write(self, path: str, content: str) -> int:
        self._check_kind("file")
        return self.sandbox.write_file(path, content.encode("utf-8"), overwrite=True)

    def file_read(self, path: str) -> str:
        self._check_kind("file")
        return self.sandbox.read_file(path).decode("utf-8", errors="replace")

    def file_list(self, path: str = ".") -> list[str]:
        self._check_kind("file")
        return self.sandbox.list_dir(path)

    def file_delete(self, path: str) -> None:
        self._check_kind("file")
        self.sandbox.delete_file(path)

    # --- жизненный цикл ---

    def _close_typed(self, name: str, cls: type, kind: str) -> None:
        # the name is freed before close(): a failing close must not leave a
        # half-closed transport occupying the name
        with self._lock:
            t = self._get(name)
            if not isinstance(t, cls):
                raise KeyError(f"transport {name!r} is not a {kind} transport")
            del self._transports[name]
            t.close()

    def serial_close(self, name: str) -> None:
        self._close_typed(name, SerialTransport, "serial")

    def modbus_close(self, name: str) -> None:
        self._close_typed(name, ModbusTransport, "modbus")

    def mqtt_close(self, name: str) -> None:
        self._close_typed(name, MqttTransport, "mqtt")

    def close_transport(self, name: str) -> None:
        with self._lock:
            t = self._transports.pop(name)
        t.close()

    def close(self) -> None:
        """Closes all transports and the journal. A failing transport close is
        collected and re-raised only after everything else (including the
        journal) has been closed - one stuck port must not leak the rest of the
        session."""
        first_error: BaseException | None = None
        with self._lock:
            transports = list(self._transports.values())
            self._transports.clear()
        for t in transports:
            try:
                t.close()
            except Exception as e:  # noqa: BLE001 - one bad port must not leak the rest
                if first_error is None:
                    first_error = e
        try:
            self.journal.close()
        except (OSError, ValueError) as e:
            if first_error is None:
                first_error = e
        if first_error is not None:
            raise first_error
