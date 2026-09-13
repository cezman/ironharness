"""Serial-транспорт io-core: обёртка над pyserial с таймаутами и хуком событий.

Понимает всё, что умеет serial_for_url: COM-порты и /dev/tty*, loop://
(программная петля — для тестов без железа), сокеты. Каждая операция уходит
в хук on_event — к нему подключён JSONL-журнал (этап 1, задача 2). Проваленная
операция тоже уходит в журнал (kind `<успех>_failed` + error) перед тем, как
исключение уходит наружу: "no log = didn't happen" касается и отказов.
"""

from __future__ import annotations

import math
import queue
import time
from collections.abc import Callable
from typing import Any, Self

import serial

from io_core.errors import TransportClosedError, TransportIoError

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
        if self._serial is None:
            raise TransportClosedError("port is not open")
        try:
            n = self._serial.write(data)
        except OSError as e:  # SerialTimeoutException at write_timeout, port errors
            self._emit("write_failed", {"data_hex": data.hex(), "error": str(e)})
            raise
        except queue.Full as e:
            # loop:// family: a full internal buffer is NOT an OSError (IH-34) -
            # without this handler the failure escaped unjournaled and surfaced
            # as an anonymous tool error. Wrapped into TransportIoError (an
            # OSError subclass) so existing handlers keep catching it.
            self._emit("write_failed", {"data_hex": data.hex(), "error": f"queue.Full: {e}"})
            raise TransportIoError(f"port buffer overflow on write: {e}") from e
        self._emit("write", {"data_hex": data.hex(), "bytes": n})
        return n

    def read(self, size: int = 1) -> bytes:
        # До size байт; по таймауту возвращает то, что успело накопиться (может b"")
        if self._serial is None:
            raise TransportClosedError("port is not open")
        try:
            data = self._serial.read(size)
        except OSError as e:
            self._emit("read_failed", {"size": size, "error": str(e)})
            raise
        except queue.Empty as e:  # IH-34: defensive symmetry with write; live pyserial
            # backends swallow Empty internally (read returns b""), the handler
            # exists so a backend that does raise it can never skip the journal
            self._emit("read_failed", {"size": size, "error": f"queue.Empty: {e}"})
            raise TransportIoError(f"port buffer underflow on read: {e}") from e
        self._emit("read", {"data_hex": data.hex(), "bytes": len(data)})
        return data

    def read_line(self, max_len: int = 256) -> bytes:
        # Читает до \n включительно; по таймауту — что успело прийти
        if self._serial is None:
            raise TransportClosedError("port is not open")
        try:
            data = self._serial.read_until(b"\n", size=max_len)
        except OSError as e:
            self._emit("read_line_failed", {"max_len": max_len, "error": str(e)})
            raise
        except queue.Empty as e:  # IH-34: defensive symmetry, see read()
            self._emit("read_line_failed", {"max_len": max_len, "error": f"queue.Empty: {e}"})
            raise TransportIoError(f"port buffer underflow on read_line: {e}") from e
        self._emit("read_line", {"data_hex": data.hex(), "bytes": len(data)})
        return data

    @property
    def in_waiting(self) -> int:
        """Bytes in the receive buffer (IH-40): lets the background reader
        poll for data instead of blocking in read() for the transport's whole
        read timeout. Not journaled - the reader polls it up to ~20 times a
        second; journaling would flood the event stream."""
        if self._serial is None:
            raise TransportClosedError("port is not open")
        try:
            return self._serial.in_waiting
        except OSError as e:
            self._emit("read_failed", {"error": f"in_waiting: {e}"})
            raise

    def cancel_read(self) -> None:
        """Aborts an in-flight blocking read() from another thread (IH-40):
        lets SerialReader.stop() confirm the thread dead promptly instead of
        waiting out the transport's read timeout. No-op when the port is not
        open (nothing can be in flight)."""
        if self._serial is None:
            return
        try:
            self._serial.cancel_read()
        except OSError as e:
            self._emit("read_failed", {"error": f"cancel_read: {e}"})
            raise

    def reset(self, *, pulse_sec: float = 0.1, settle_sec: float = 2.0) -> None:
        """Управляемый сброс платы: импульс RTS (asserted на pulse_sec) при
        отпущенном DTR (IH-19).

        Классическая обвязка ESP32: RTS через транзисторную пару тянет EN
        (reset), DTR тянет IO0 (boot mode). Утверждённый RTS при отпущенном
        DTR перезапускает чип в нормальный flash-boot (не download mode) —
        чистое состояние между попытками solve или после зависания REPL.
        open() сброс НЕ делает (idle-фикс 9e836d7: открытие порта не должно
        пиновать плату); сброс — только по явному вызову. Физический эффект
        есть только на портах с modem-линиями (реальный USB-UART); loop://
        и socket:// принимают присваивания как no-op (журнал честно
        отметить это не может), ptys дают OSError — отказ журналируется
        (reset_failed) и пробрасывается.

        IH-43: pulse_sec/settle_sec валидируются ДО воздействия на линии
        (конечные, неотрицательные, в разумном верхнем пределе), а линии
        освобождаются в finally — раньше time.sleep(-1) бросал ValueError
        после rts=True, обработчик ловил только OSError, и плата оставалась
        удержанной в сбросе после неуспешного вызова.
        """
        if self._serial is None:
            raise TransportClosedError("port is not open")
        error = self._validate_reset_durations(pulse_sec, settle_sec)
        if error is not None:
            self._emit("reset_failed", {"error": error})
            raise ValueError(error)
        try:
            self._serial.dtr = False  # IO0 high: normal boot, не download mode
            self._serial.rts = True  # EN low: удерживаем чип в сбросе
            time.sleep(pulse_sec)
        except OSError as e:
            self._emit("reset_failed", {"error": str(e)})
            raise
        finally:
            try:
                self._serial.rts = False  # EN high: чип стартует - в любом случае
            except OSError:
                pass  # мёртвый порт и линию не держит - отказ уже в журнале
        time.sleep(settle_sec)  # boot-тишина: вывод стартующей прошивки пойдёт в ридер/чтения
        self._emit("reset", {"pulse_sec": pulse_sec, "settle_sec": settle_sec})

    @staticmethod
    def _validate_reset_durations(pulse_sec: float, settle_sec: float) -> str | None:
        """None = допустимы; иначе текст отказа (IH-43)."""
        for name, value, cap in (
            ("pulse_sec", pulse_sec, 5.0),
            ("settle_sec", settle_sec, 60.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{name} must be a number, got {value!r}"
            if not math.isfinite(value):
                return f"{name} must be finite, got {value!r}"
            if value < 0:
                return f"{name} must be >= 0, got {value!r}"
            if value > cap:
                return f"{name} must be <= {cap:g} s, got {value!r}"
        return None
