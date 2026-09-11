"""Serial-транспорт io-core: обёртка над pyserial с таймаутами и хуком событий.

Понимает всё, что умеет serial_for_url: COM-порты и /dev/tty*, loop://
(программная петля — для тестов без железа), сокеты. Каждая операция уходит
в хук on_event — к нему подключён JSONL-журнал (этап 1, задача 2). Проваленная
операция тоже уходит в журнал (kind `<успех>_failed` + error) перед тем, как
исключение уходит наружу: "no log = didn't happen" касается и отказов.
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
        try:
            self._serial = serial.serial_for_url(
                self._port,
                baudrate=self._baudrate,
                timeout=self._timeout,
                write_timeout=self._write_timeout,
            )
            # Real hardware: pyserial asserts DTR/RTS on open, which pulses a reset
            # on live CH340 boards. Idle lines are best-effort — ptys reject modem
            # ioctls (OSError), so never let line control break open().
            try:
                self._serial.dtr = False
                self._serial.rts = False
            except OSError:
                pass
        except (OSError, ValueError) as e:
            # ValueError: unknown URL protocol (agent's port string is untrusted)
            self._emit("open_failed", {"port": self._port, "error": str(e)})
            raise
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
        assert self._serial is not None, "port is not open"
        try:
            n = self._serial.write(data)
        except OSError as e:  # SerialTimeoutException at write_timeout, port errors
            self._emit("write_failed", {"data_hex": data.hex(), "error": str(e)})
            raise
        self._emit("write", {"data_hex": data.hex(), "bytes": n})
        return n

    def read(self, size: int = 1) -> bytes:
        # До size байт; по таймауту возвращает то, что успело накопиться (может b"")
        assert self._serial is not None, "port is not open"
        try:
            data = self._serial.read(size)
        except OSError as e:
            self._emit("read_failed", {"size": size, "error": str(e)})
            raise
        self._emit("read", {"data_hex": data.hex(), "bytes": len(data)})
        return data

    def read_line(self, max_len: int = 256) -> bytes:
        # Читает до \n включительно; по таймауту — что успело прийти
        assert self._serial is not None, "port is not open"
        try:
            data = self._serial.read_until(b"\n", size=max_len)
        except OSError as e:
            self._emit("read_line_failed", {"max_len": max_len, "error": str(e)})
            raise
        self._emit("read_line", {"data_hex": data.hex(), "bytes": len(data)})
        return data
