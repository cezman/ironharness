"""Лимиты операций: rate-limit и дедлайн (этап 1, задача 5).

Обёртки композируются с любым транспортом:
    RateLimitedTransport(DeadlineTransport(SerialTransport(...), seconds=30), limiter)
Часы инъекцируются (clock) — тесты детерминированные, без реальных ожиданий.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Self

from io_core.errors import OperationTimeout, RateLimitExceeded

Clock = Callable[[], float]

TRANSPORT_DEADLINE_ENV = "IRONHARNESS_TRANSPORT_DEADLINE"
TRANSPORT_RATE_ENV = "IRONHARNESS_TRANSPORT_RATE"
DEFAULT_TRANSPORT_DEADLINE_SEC = 600.0


def parse_transport_deadline(raw: str | None) -> float | None:
    """Deadline for transport operations in seconds.

    None/empty -> the default (600 s); "0" or "off" -> disabled; otherwise a
    positive number of seconds. Garbage raises ValueError (fail loud).
    """
    if raw is None or not raw.strip():
        return DEFAULT_TRANSPORT_DEADLINE_SEC
    if raw.strip().lower() in ("0", "off"):
        return None
    seconds = float(raw)
    if seconds <= 0:
        raise ValueError(f"{TRANSPORT_DEADLINE_ENV} must be > 0, or 0/off to disable")
    return seconds


def parse_transport_rate(raw: str | None) -> tuple[int, float] | None:
    """Parses IRONHARNESS_TRANSPORT_RATE as 'max_calls/window_seconds'
    (e.g. "100/60"). None/empty -> no rate limiting; garbage -> ValueError."""
    if raw is None or not raw.strip():
        return None
    max_s, sep, per_s = raw.partition("/")
    if not sep:
        raise ValueError(f"{TRANSPORT_RATE_ENV} must be max_calls/window_seconds, got {raw!r}")
    max_calls, per_seconds = int(max_s), float(per_s)
    if max_calls < 1 or per_seconds <= 0:
        raise ValueError(f"{TRANSPORT_RATE_ENV} must be max_calls >= 1 and window > 0")
    return max_calls, per_seconds


class RateLimiter:
    """Фиксированное окно: не более max_calls за per_seconds, иначе исключение.

    Потокобезопасен (IH-12): окно и счётчик меняются под локом, иначе
    параллельные acquire() проходят проверку-затем-инкремент одновременно.
    """

    def __init__(self, max_calls: int, per_seconds: float, clock: Clock = time.monotonic) -> None:
        self._max_calls = max_calls
        self._per = per_seconds
        self._clock = clock
        self._window_start: float | None = None
        self._used = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            if self._window_start is None or now - self._window_start >= self._per:
                self._window_start = now
                self._used = 0
            self._used += 1
            if self._used > self._max_calls:
                raise RateLimitExceeded(
                    f"операция #{self._used} за окно {self._per}s (лимит {self._max_calls})"
                )


class RateLimitedTransport:
    """Пропускает операции I/O через RateLimiter; open/close не лимитируются.
    Всё прочее (modbus_read, mqtt_*, ...) пробрасывается в обёрнутый транспорт
    через __getattr__ - обёртки прозрачны для любого вида транспорта (IH-17)."""

    def __init__(self, transport: Any, limiter: RateLimiter) -> None:
        self._t = transport
        self._limiter = limiter

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._t, name)

    def write(self, data: bytes) -> int:
        self._limiter.acquire()
        return self._t.write(data)

    def read(self, size: int = 1) -> bytes:
        self._limiter.acquire()
        return self._t.read(size)

    def read_line(self, max_len: int = 256) -> bytes:
        self._limiter.acquire()
        return self._t.read_line(max_len)

    def __enter__(self) -> Self:
        self._t.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self._t.close()


class DeadlineTransport:
    """Роняет операции OperationTimeout, когда истёк дедлайн с момента open().
    Неопределённые атрибуты пробрасываются в обёрнутый транспорт (IH-17)."""

    def __init__(self, transport: Any, seconds: float, clock: Clock = time.monotonic) -> None:
        self._t = transport
        self._seconds = seconds
        self._clock = clock
        self._started: float | None = None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._t, name)

    def _check(self) -> None:
        assert self._started is not None, "transport is not open"
        if self._clock() - self._started > self._seconds:
            raise OperationTimeout(f"deadline of {self._seconds}s expired")

    def open(self) -> None:
        self._t.open()
        self._started = self._clock()

    def close(self) -> None:
        self._t.close()

    def write(self, data: bytes) -> int:
        self._check()
        return self._t.write(data)

    def read(self, size: int = 1) -> bytes:
        self._check()
        return self._t.read(size)

    def read_line(self, max_len: int = 256) -> bytes:
        self._check()
        return self._t.read_line(max_len)

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
