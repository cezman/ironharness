"""Serial-транспорт io-core: обёртка над pyserial с таймаутами и хуком событий.

Понимает всё, что умеет serial_for_url: COM-порты и /dev/tty*, loop://
(программная петля — для тестов без железа), сокеты. Каждая операция уходит
в хук on_event — к нему позже подключится JSONL-журнал (этап 1, задача 2).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Self

import serial

EventHook = Callable[[str, dict[str, Any]], None]


class SerialTransport:
    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        timeout: float = 1.0,
        write_timeout: float = 1.0,
        on_event: EventHook | None = None,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._write_timeout = write_timeout
        self._on_event = on_event
        self._serial: serial.SerialBase | None = None

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, data)

    def open(self) -> None:
        self._serial = serial.serial_for_url(
            self._port,
            baudrate=self._baudrate,
            timeout=self._timeout,
            write_timeout=self._write_timeout,
        )
        # Реальное железо: pyserial взводит DTR/RTS при открытии, у CH340-плат
        # это импульс сброса. Явные idle-линии делают поведение детерминированным.
        self._serial.dtr = False
        self._serial.rts = False
        self._emit("open", {"port": self._port, "baudrate": self._baudrate})

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None
            self._emit("close", {})

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def write(self, data: bytes) -> int:
        assert self._serial is not None, "порт не открыт"
        n = self._serial.write(data)
        self._emit("write", {"data_hex": data.hex(), "bytes": n})
        return n

    def read(self, size: int = 1) -> bytes:
        # До size байт; по таймауту возвращает то, что успело накопиться (может b"")
        assert self._serial is not None, "порт не открыт"
        data = self._serial.read(size)
        self._emit("read", {"data_hex": data.hex(), "bytes": len(data)})
        return data

    def read_line(self, max_len: int = 256) -> bytes:
        # Читает до \n включительно; по таймауту — что успело прийти
        assert self._serial is not None, "порт не открыт"
        data = self._serial.read_until(b"\n", size=max_len)
        self._emit("read_line", {"data_hex": data.hex(), "bytes": len(data)})
        return data
