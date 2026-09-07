"""Сессия агента: именованные транспорты + песочница + единый журнал (этап 1, задача 7).

Единица работы агента: MCP-инструменты оперируют сессией, каждая операция
автоматически попадает в журнал сессии (конвенция «без лога = не выполнено»).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from io_core.file_sandbox import FileSandbox
from io_core.journal import JsonlJournal
from io_core.modbus_transport import ModbusTransport
from io_core.mqtt_transport import MqttTransport
from io_core.serial_transport import SerialTransport


class Session:
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

    def _check_free(self, name: str) -> None:
        if name in self._transports:
            raise KeyError(f"транспорт {name!r} уже открыт")

    def _get(self, name: str) -> Any:
        try:
            return self._transports[name]
        except KeyError:
            raise KeyError(f"транспорт {name!r} не открыт") from None

    # --- serial ---

    def serial_open(
        self, name: str, port: str, *, baudrate: int = 115200, timeout: float = 1.0
    ) -> None:
        self._check_free(name)
        t = SerialTransport(port, baudrate=baudrate, timeout=timeout, on_event=self.journal)
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
        self._check_free(name)
        t = ModbusTransport(
            host, port=port, device_id=device_id, timeout=timeout, on_event=self.journal
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
        self._check_free(name)
        t = MqttTransport(
            host, port=port, client_id=client_id, timeout=timeout, on_event=self.journal
        )
        t.open()
        self._transports[name] = t

    def mqtt_publish(self, name: str, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> None:
        self._get(name).publish(topic, payload, qos=qos, retain=retain)

    def mqtt_subscribe(self, name: str, topic: str, *, qos: int = 0) -> None:
        self._get(name).subscribe(topic, qos=qos)

    def mqtt_read(self, name: str, timeout: float = 1.0) -> dict[str, str] | None:
        return self._get(name).read_message(timeout)

    # --- файлы (песочница) ---

    def file_write(self, path: str, content: str) -> int:
        return self.sandbox.write_file(path, content.encode("utf-8"), overwrite=True)

    def file_read(self, path: str) -> str:
        return self.sandbox.read_file(path).decode("utf-8", errors="replace")

    def file_list(self, path: str = ".") -> list[str]:
        return self.sandbox.list_dir(path)

    def file_delete(self, path: str) -> None:
        self.sandbox.delete_file(path)

    # --- жизненный цикл ---

    def close_transport(self, name: str) -> None:
        self._get(name).close()
        del self._transports[name]

    def close(self) -> None:
        for t in self._transports.values():
            t.close()
        self._transports.clear()
        self.journal.close()
