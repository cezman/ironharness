"""JSONL-журнал операций io-core (этап 1, задача 2).

Одна строка файла — одно событие: ts (epoch), seq (порядковый номер),
actor (кто), kind (тип события) + payload. Совместим с хуком on_event
транспортов: экземпляр журнала передаётся прямо в SerialTransport.

Целостность (IH-32): payload не может затереть служебные ключи
(ts/seq/actor/kind/conn) — конфликтующие ключи отбрасываются с событием
journal_key_conflict, а не молча ломают реплеер/вьюер. Межпроцессная запись
сериализуется файловой блокировкой: потоки одного инстанса — под внутренним
локом, процессы (второй агент на том же IRONHARNESS_HOME) — под advisory
lock на весь write+flush, так что O_APPEND-гонка Windows больше не теряет
строки.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Self

Event = dict[str, Any]

# Служебные ключи записи: payload не может их затереть (IH-32). conn не здесь:
# его атрибуция — забота хука сессии, который теперь ставит conn ПОСЛЕДНИМ
# ({**data, "conn": name}), так что payload события не может затереть имя
# соединения даже случайно.
RESERVED_KEYS = frozenset({"ts", "seq", "actor", "kind"})


def _lock_file_region(fh: Any, pos: int, length: int) -> None:
    """Advisory lock over the byte range [pos, pos+length) - the exact region
    our line will occupy. Windows: msvcrt.locking blocks until the other
    process releases; POSIX: flock is an exclusive whole-file lock. The SAME
    explicit range must be used for the unlock (recomputing "end" after the
    write would target a different range and leak the lock forever)."""
    if sys.platform == "win32":
        import msvcrt

        fh.seek(pos)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, max(1, length))
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _unlock_file_region(fh: Any, pos: int, length: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(pos)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, max(1, length))
        except OSError:
            pass  # already released by close() during interpreter teardown
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class JsonlJournal:
    def __init__(self, path: str | Path, actor: str = "io_core") -> None:
        self._path = Path(path)
        self._actor = actor
        self._seq = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # колбэки MQTT пишут из сетевого потока paho
        # Режим "a": межпроцессные записи сериализуются файловой блокировкой
        # (_lock_file_region) на весь write+flush — O_APPEND между хэндлами на
        # Windows не атомарен (аудит перед v0.7.0: два процесса теряли 5-12
        # строк из 1000), теперь потеря невозможна.
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
            conflicts = {k: v for k, v in data.items() if k in RESERVED_KEYS}
            rec.update({k: v for k, v in data.items() if k not in conflicts})
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            n_bytes = len(line.encode("utf-8"))
            # the file lock spans write+flush over the EXACT region our line
            # occupies: a second process cannot interleave its seek-append
            # between our reservation and our flush
            self._fh.seek(0, os.SEEK_END)
            region = self._fh.tell()
            _lock_file_region(self._fh, region, n_bytes)
            try:
                self._fh.write(line)
                self._fh.flush()
            finally:
                _unlock_file_region(self._fh, region, n_bytes)
            if conflicts:
                # the conflict is journaled AFTER the main event (IH-32): the
                # event keeps its identity and its seq order
                self._fh.write(
                    json.dumps(
                        {
                            "ts": round(time.time(), 3),
                            "seq": self._seq + 1,
                            "actor": self._actor,
                            "kind": "journal_key_conflict",
                            "requested_kind": kind,
                            "keys": sorted(conflicts),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                self._seq += 1

    def close(self) -> None:
        with self._lock:  # колбэк из сетевого потока не должен писать в закрытый файл
            self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: str | Path) -> list[Event]:
    """Читает JSONL-журнал в список событий. Битая строка (не JSON, не объект)
    — JournalCorrupt с именем файла, а не KeyError где-то в потребителе."""
    from io_core.errors import JournalCorrupt

    events: list[Event] = []
    with Path(path).open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError as e:
                raise JournalCorrupt(f"{path}:{n}: line is not valid JSON: {e}") from None
            if not isinstance(rec, dict):
                raise JournalCorrupt(f"{path}:{n}: line is not a JSON object")
            events.append(rec)
    return events
