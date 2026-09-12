"""JSONL-журнал операций io-core (этап 1, задача 2).

Одна строка файла — одно событие: ts (epoch), seq (порядковый номер),
actor (кто), kind (тип события) + payload. Совместим с хуком on_event
транспортов: экземпляр журнала передаётся прямо в SerialTransport.

Целостность (IH-32): payload не может затереть служебные ключи
(ts/seq/actor/kind) — конфликтующие ключи отбрасываются с событием
journal_key_conflict, а не молча ломают реплеер/вьюер (conn защищён хуком
сессии, который ставит его ПОСЛЕ payload). Межпроцессная запись
сериализуется sidecar lock-файлом (<path>.lock): все писатели лочат ОДИН
фиксированный байт — O_APPEND-гонка Windows (аудит перед v0.7.0: два
процесса теряли 5-12 строк из 1000) невозможна. Sidecar выбран вместо лока
на диапазон самого журнала: конец файла под "a"-режимом уезжает, пока
писатель ждёт чужой лок (stale-якорь), и запись попадает в чужой диапазон.
Писатель, умерший между lock/unlock, не вешает журнал: ОС снимает лок при
закрытии хэндла. msvcrt.LK_LOCK сдаётся после ~10 c ожидания — под
 pathological долгим удержанием запись упадёт с OSError (громко, не молча).
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
# его атрибуция — забота хука сессии, который ставит conn ПОСЛЕДНИМ
# ({**data, "conn": name}), так что payload события не может затереть имя
# соединения даже случайно.
RESERVED_KEYS = frozenset({"ts", "seq", "actor", "kind"})


def _acquire_lock_file(path: Path) -> Any:
    """Открывает (создаёт) sidecar lock-файл и берёт эксклюзивный лок на
    его первый байт: фиксированный диапазон = общий мьютекс всех писателей."""
    fh = path.open("a+b")
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def _release_lock_file(fh: Any) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # already released by close() during interpreter teardown
    finally:
        fh.close()


class JsonlJournal:
    def __init__(self, path: str | Path, actor: str = "io_core") -> None:
        self._path = Path(path)
        self._actor = actor
        self._seq = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # колбэки MQTT пишут из сетевого потока paho
        # Sidecar lock-файл создаётся в конструкторе, чтобы провалиться рано
        # (read-only том и т.п.) — а не при первом событии.
        self._lock_path = self._path.with_name(self._path.name + ".lock")
        with _acquire_lock_file(self._lock_path):
            pass
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
            if conflicts:
                line += json.dumps(
                    {
                        "ts": round(time.time(), 3),
                        "seq": self._seq + 1,
                        "actor": self._actor,
                        "kind": "journal_key_conflict",
                        "requested_kind": kind,
                        "keys": sorted(conflicts),
                    },
                    ensure_ascii=False,
                ) + "\n"
            # both lines go out under ONE sidecar lock: a second process
            # cannot interleave its append between ours
            lock_fh = _acquire_lock_file(self._lock_path)
            try:
                self._fh.seek(0, os.SEEK_END)
                self._fh.write(line)
                self._fh.flush()
                self._seq += 1 if conflicts else 0
            finally:
                _release_lock_file(lock_fh)

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
