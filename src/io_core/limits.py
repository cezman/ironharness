"""Лимиты операций: rate-limit и дедлайн (этап 1, задача 5).

Обёртки композируются с любым транспортом:
    RateLimitedTransport(DeadlineTransport(SerialTransport(...), seconds=30), limiter)
Часы инъекцируются (clock) — тесты детерминированные, без реальных ожиданий.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Self

from io_core.errors import OperationTimeout, RateLimitExceeded

Clock = Callable[[], float]


class RateLimiter:
    """Фиксированное окно: не более max_calls за per_seconds, иначе исключение."""

    def __init__(self, max_calls: int, per_seconds: float, clock: Clock = time.monotonic) -> None:
        self._max_calls = max_calls
        self._per = per_seconds
        self._clock = clock
        self._window_start: float | None = None
        self._used = 0

    def acquire(self) -> None:
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
    """Пропускает операции I/O через RateLimiter; open/close не лимитируются."""

    def __init__(self, transport: Any, limiter: RateLimiter) -> None:
        self._t = transport
        self._limiter = limiter

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
    """Роняет операции OperationTimeout, когда истёк дедлайн с момента open()."""

    def __init__(self, transport: Any, seconds: float, clock: Clock = time.monotonic) -> None:
        self._t = transport
        self._seconds = seconds
        self._clock = clock
        self._started: float | None = None

    def _check(self) -> None:
        assert self._started is not None, "транспорт не открыт"
        if self._clock() - self._started > self._seconds:
            raise OperationTimeout(f"дедлайн {self._seconds}s истёк")

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
