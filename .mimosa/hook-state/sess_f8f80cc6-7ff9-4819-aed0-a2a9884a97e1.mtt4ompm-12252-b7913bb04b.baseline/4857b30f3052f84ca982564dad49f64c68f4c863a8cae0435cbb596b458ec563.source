"""JSONL-журнал операций io-core (этап 1, задача 2).

Одна строка файла — одно событие: ts (epoch), seq (порядковый номер),
actor (кто), kind (тип события) + payload. Совместим с хуком on_event
транспортов: экземпляр журнала передаётся прямо в SerialTransport.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Self

Event = dict[str, Any]


class JsonlJournal:
    def __init__(self, path: str | Path, actor: str = "io_core") -> None:
        self._path = Path(path)
        self._actor = actor
        self._seq = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # колбэки MQTT пишут из сетевого потока paho
        # Режим "a": несколько сессий могут дописывать один файл
        self._fh = self._path.open("a", encoding="utf-8")

    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            rec: Event = {
                "ts": round(time.time(), 3),
                "seq": self._seq,
                "actor": self._actor,
                "kind": kind,
            }
            rec.update(data)
            self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:  # колбэк из сетевого потока не должен писать в закрытый файл
            self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: str | Path) -> list[Event]:
    """Читает JSONL-журнал в список событий."""
    with Path(path).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
