"""Внедрение сбоев в транспорт: обрывы, задержки, порча и потеря байтов.

Скриптованный детерминированный сценарий (практика HIL-стендов): список событий
Fault, каждое активируется на операциях после after_ops с вероятностью probability.
FaultyTransport — обёртка над любым транспортом с write/read, как RateLimitedTransport.
Сценарий нужен для проверки агентов и прошивок на шумных/ненадёжных линиях:
битые кадры, потеря пакетов, обрыв связи — детерминированно при фиксированном seed.
"""

from __future__ import annotations

import dataclasses
import random
import time
from collections.abc import Callable, Iterable
from typing import Any

from io_core.errors import ConnectionLost

ACTIONS = ("drop", "corrupt", "delay", "disconnect")


@dataclasses.dataclass(frozen=True)
class Fault:
    """Событие сбоя.

    action: drop (запись теряется), corrupt (инверсия случайных битов), delay
    (задержка seconds), disconnect (ConnectionLost).
    after_ops: сбой активен на операциях с номером (1-based) больше after_ops.
    probability: шанс срабатывания на каждой активной операции.
    ratio: доля байтов, подвергаемых порче (для corrupt).
    """

    action: str
    after_ops: int = 0
    probability: float = 1.0
    ratio: float = 0.25
    seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"неизвестное действие {self.action!r}; разрешены: {ACTIONS}")
        if not 0 <= self.probability <= 1:
            raise ValueError("probability должна быть в [0, 1]")
        if not 0 <= self.ratio <= 1:
            raise ValueError("ratio должна быть в [0, 1]")
        if self.seconds < 0:
            raise ValueError("seconds должна быть >= 0")
        if self.after_ops < 0:
            raise ValueError("after_ops должна быть >= 0")


def _corrupt(data: bytes, ratio: float, rng: random.Random) -> bytes:
    """Инвертирует случайные биты в доле ratio байтов; длину не меняет."""
    if not data or ratio <= 0:
        return data
    count = max(1, min(int(len(data) * ratio) or 1, len(data)))
    positions = rng.sample(range(len(data)), count)
    out = bytearray(data)
    for pos in positions:
        out[pos] ^= 1 << rng.randrange(8)
    return bytes(out)


class FaultyTransport:
    """Обёртка: вносит сбои по сценарию в write/read целевого транспорта.

    rng и sleep инъецируются для детерминированных тестов (sleep-заглушка
    вместо настоящей задержки). Неизвестные атрибуты пробрасываются в target.
    """

    def __init__(
        self,
        target: Any,
        faults: Iterable[Fault],
        *,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._target = target
        self._faults = [f if isinstance(f, Fault) else Fault(**f) for f in faults]
        self._rng = rng or random.Random(0)
        self._sleep = sleep
        self.ops = 0  # счётчик операций write+read, 1-based

    def _active(self) -> list[Fault]:
        self.ops += 1
        return [
            f
            for f in self._faults
            if self.ops > f.after_ops and self._rng.random() < f.probability
        ]

    def write(self, data: bytes) -> None:
        faults = self._active()
        if any(f.action == "disconnect" for f in faults):
            raise ConnectionLost(f"обрыв по сценарию на операции {self.ops}")
        for f in faults:
            if f.action == "delay":
                self._sleep(f.seconds)
        for f in faults:
            if f.action == "drop":
                return  # байты не доходят до цели и не считаются ошибкой
        for f in faults:
            if f.action == "corrupt":
                data = _corrupt(data, f.ratio, self._rng)
        self._target.write(data)

    def read(self, size: int) -> bytes:
        faults = self._active()
        if any(f.action == "disconnect" for f in faults):
            raise ConnectionLost(f"обрыв по сценарию на операции {self.ops}")
        for f in faults:
            if f.action == "delay":
                self._sleep(f.seconds)
        if any(f.action == "drop" for f in faults):
            return b""  # данные «потерялись в линии»
        return self._target.read(size)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)
