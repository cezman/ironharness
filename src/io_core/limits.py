"""Лимиты операций: rate-limit и дедлайн (этап 1, задача 5).

Обёртки композируются с любым транспортом:
    RateLimitedTransport(DeadlineTransport(SerialTransport(...), seconds=30), limiter)
Часы инъекцируются (clock) — тесты детерминированные, без реальных ожиданий.
"""

from __future__ import annotations

import math
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
    if not math.isfinite(seconds) or seconds <= 0:
        # nan/inf would silently disable the deadline comparison
        raise ValueError(f"{TRANSPORT_DEADLINE_ENV} must be a finite number > 0, or 0/off")
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
    if not (1 <= max_calls <= 10**9) or not math.isfinite(per_seconds) or per_seconds <= 0:
        raise ValueError(f"{TRANSPORT_RATE_ENV} must be max_calls >= 1 and a finite window > 0")
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
    """Пропускает операции I/O через RateLimiter: write/read/read_line напрямую,
    любой другой вызываемый атрибут (modbus_read, mqtt_publish, ...) - через
    форвардинг с тем же лимитом. open/close не лимитируются. on_event (IH-32)
    журналирует отказы лимита — «no log = didn't happen» касается и отказов."""

    def __init__(self, transport: Any, limiter: RateLimiter, on_event: Callable | None = None) -> None:
        self._t = transport
        self._limiter = limiter
        self._on_event = on_event

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._t, name)
        if not callable(attr):
            return attr

        def forwarded(*args: Any, **kwargs: Any) -> Any:
            try:
                self._limiter.acquire()
            except RateLimitExceeded as e:
                if self._on_event is not None:
                    self._on_event("rate_denied", {"error": str(e)})
                raise
            return attr(*args, **kwargs)

        return forwarded

    def open(self) -> None:
        self._t.open()

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> Self:
        self._t.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self._t.close()


class DeadlineTransport:
    """Роняет операции OperationTimeout, когда истёк дедлайн БЕЗДЕЙСТВИЯ.

    Скользящее окно (IH-35): каждая УСПЕШНАЯ операция сдвигает дедлайн, так
    что живая интерактивная сессия (редкие, но успешные обращения) работает
    сколь угодно долго, а транспорт, по которому не было успешных операций
    `seconds` подряд, требует переоткрытия — прежняя семантика «время от
    open()» убивала живые диагностики на 10-й минуте сессии. Операция,
    завершившаяся исключением, окно НЕ сдвигает. open/close не проверяются;
    write/read/read_line и любой другой вызываемый атрибут (modbus_read,
    mqtt_publish, ...) идут через форвардинг с проверкой+обновлением. Отказ
    журналируется через on_event (deadline_denied, IH-32) с секундами
    бездействия; label (имя соединения) попадает в текст ошибки (IH-35).
    Фоновый serial-ридер (IH-18) дренирует RAW транспорт мимо обёрток:
    живой вывод платы окно НЕ сдвигает — это осознанно (pump не агентная
    операция, «wedged»-линк, качающий мусор, должен состариться), поэтому
    возможен denial при живом tail; текст ошибки называет соединение.
    """

    def __init__(self, transport: Any, seconds: float, clock: Clock = time.monotonic,
                 on_event: Callable | None = None, label: str | None = None) -> None:
        self._t = transport
        self._seconds = seconds
        self._clock = clock
        self._started: float | None = None
        self._on_event = on_event
        self._label = label

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._t, name)
        if not callable(attr):
            return attr

        def forwarded(*args: Any, **kwargs: Any) -> Any:
            self._check()
            result = attr(*args, **kwargs)
            self._started = self._clock()  # сдвиг окна: только успех
            return result

        return forwarded

    def _check(self) -> None:
        if self._started is None:  # explicit, not assert: survives python -O (IH-32)
            from io_core.errors import TransportClosedError

            raise TransportClosedError("transport is not open")
        now = self._clock()
        if now - self._started > self._seconds:
            if self._on_event is not None:
                self._on_event(
                    "deadline_denied",
                    {"deadline_sec": self._seconds, "idle_sec": round(now - self._started, 3)},
                )
            target = f" on connection {self._label!r}" if self._label else ""
            raise OperationTimeout(
                f"no successful operation within {self._seconds}s{target} - "
                "the connection went stale, close and reopen it to reset the deadline"
            )

    def open(self) -> None:
        self._t.open()
        self._started = self._clock()

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
