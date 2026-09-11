"""Modbus TCP транспорт io-core: holding-регистры с журналированием (этап 1, задача 3).

Клиент поверх pymodbus. Каждая операция уходит в on_event (тот же контракт,
что у SerialTransport) — журнал/реплей подключаются без изменений. Проваленная
операция тоже уходит в журнал (kind `<успех>_failed` + error) перед тем, как
исключение уходит наружу: "no log = didn't happen" касается и отказов.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Self

from pymodbus.client import ModbusTcpClient

from io_core.policy import AccessPolicy

EventHook = Callable[[str, dict[str, Any]], None]


class ModbusTransport:
    def __init__(
        self,
        host: str,
        *,
        port: int = 502,
        device_id: int = 1,
        timeout: float = 3.0,
        on_event: EventHook | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._device_id = device_id
        self._timeout = timeout
        self._on_event = on_event
        self._client: ModbusTcpClient | None = None

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, data)

    def open(self) -> None:
        AccessPolicy.from_env(on_event=self._on_event).check_host(self._host, self._port)
        try:
            self._client = ModbusTcpClient(self._host, port=self._port, timeout=self._timeout)
            if not self._client.connect():
                raise ConnectionError(f"failed to connect to {self._host}:{self._port}")
        except OSError as e:
            # policy refusals are journaled by the policy itself; connect
            # failures are ours to record
            self._emit(
                "modbus_open_failed",
                {"host": self._host, "port": self._port, "error": str(e)},
            )
            raise
        self._emit("modbus_open", {"host": self._host, "port": self._port})

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._emit("modbus_close", {})

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_client(self) -> ModbusTcpClient:
        assert self._client is not None, "connection is not open"
        return self._client

    def _check(self, result: Any, op: str) -> None:
        if result.isError():
            raise OSError(f"modbus {op}: {result}")

    def read_holding(self, address: int, count: int = 1) -> list[int]:
        try:
            result = self._require_client().read_holding_registers(
                address, count=count, device_id=self._device_id
            )
            self._check(result, "read_holding")
        except OSError as e:
            self._emit(
                "modbus_read_failed", {"address": address, "count": count, "error": str(e)}
            )
            raise
        values = list(result.registers)
        self._emit("modbus_read", {"address": address, "count": count, "values": values})
        return values

    def write_register(self, address: int, value: int) -> None:
        try:
            result = self._require_client().write_register(
                address, value, device_id=self._device_id
            )
            self._check(result, "write_register")
        except OSError as e:
            self._emit(
                "modbus_write_failed", {"address": address, "values": [value], "error": str(e)}
            )
            raise
        self._emit("modbus_write", {"address": address, "values": [value]})

    def write_registers(self, address: int, values: Sequence[int]) -> None:
        values = list(values)
        try:
            result = self._require_client().write_registers(
                address, values, device_id=self._device_id
            )
            self._check(result, "write_registers")
        except OSError as e:
            self._emit(
                "modbus_write_failed", {"address": address, "values": values, "error": str(e)}
            )
            raise
        self._emit("modbus_write", {"address": address, "values": values})
