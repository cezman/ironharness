"""Верификация эффектов: записал → прочитал назад → сравнил (этап 1, задача 5)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from io_core.errors import VerificationError

Clock = Callable[[], float]


def expect_read(
    transport: Any,
    expected: bytes,
    *,
    timeout: float = 5.0,
    clock: Clock = time.monotonic,
) -> bytes:
    """Читает из транспорта, пока expected не появится в накопленном буфере.

    Возвращает весь накопленный буфер (вместе с мусором до ожидания).
    VerificationError — если за timeout ожидание не подтвердилось.
    """
    if not expected:
        return b""
    deadline = clock() + timeout
    buf = bytearray()
    while expected not in buf:
        if clock() >= deadline:
            raise VerificationError(
                f"не дождались {expected!r} за {timeout}s; буфер: {bytes(buf)!r}"
            )
        chunk = transport.read(len(expected))
        if chunk:
            buf += chunk
        else:
            time.sleep(min(0.01, max(deadline - clock(), 0)))
    return bytes(buf)


def write_and_expect(
    transport: Any,
    data: bytes,
    expected: bytes | None = None,
    **kwargs: Any,
) -> bytes:
    """Запись + подтверждение чтением. По умолчанию ждём эхо записанных данных."""
    transport.write(data)
    return expect_read(transport, data if expected is None else expected, **kwargs)
