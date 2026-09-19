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
    def __init__(
        self,
        path: str | Path,
        actor: str = "io_core",
        *,
        max_bytes: int = 32 * 1024 * 1024,
        max_files: int = 5,
    ) -> None:
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
        # IH-63: ротация по размеру — длинная сессия с фоновым ридером не
        # должна расти бесконечно. Старые части уходят в .N (1 — новейшая
        # из архивных), сверх max_files — самые старые удаляются.
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._closed = False
        # Early probe: fail here on a read-only volume etc., not on the first
        # event. Under the sidecar lock: a concurrent rotation must not hit
        # this transient handle (same WinError-32 class as IH-72). No handle
        # is kept between writes: a persistent handle made rotation
        # incompatible with a second writer (Windows: replace() hit the other
        # writer's open handle -> WinError 32; POSIX: the other writer's
        # handle silently followed the rename into the archived part).
        lock_fh = _acquire_lock_file(self._lock_path)
        try:
            with self._path.open("a", encoding="utf-8"):
                pass
        finally:
            _release_lock_file(lock_fh)

    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        with self._lock:
            if self._closed:
                # session-close contract (IH-13): a closed session must not
                # journal - writes after close() fail loudly, not silently
                raise ValueError(f"journal {self._path} is closed")
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
            # cannot interleave its append between ours. The file is opened
            # per write under that lock and never held between writes (IH-72):
            # a persistent handle broke rotation against a second writer.
            lock_fh = _acquire_lock_file(self._lock_path)
            try:
                self._rotate_if_full()
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
                self._seq += 1 if conflicts else 0
            finally:
                _release_lock_file(lock_fh)

    def _rotate_if_full(self) -> None:
        """IH-63: если текущий журнал превысил max_bytes — сдвинуть части
        (.1 -> .2, ...): живой файл уходит в .1, а свежий живой файл создаёт
        следующая запись (per-write open, IH-72). Числовые части старше
        max_files удаляются. Called under the journal lock before each write."""
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self._max_bytes:
            return
        oldest = self._path.with_name(f"{self._path.name}.{self._max_files}")
        if oldest.exists():
            oldest.unlink()
        for i in range(self._max_files - 1, 0, -1):
            src = self._path.with_name(f"{self._path.name}.{i}")
            if src.exists():
                src.replace(self._path.with_name(f"{self._path.name}.{i + 1}"))
        self._path.replace(self._path.with_name(f"{self._path.name}.1"))

    def close(self) -> None:
        """Rejects further writes (the session-close contract). No handle is
        held between writes since IH-72 - close() only flips the flag."""
        with self._lock:
            self._closed = True

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


def chain_files(path: str | Path) -> list[Path]:
    """IH-75: все файлы цепочки ротации — архивные части по убыванию индекса
    (старейшая первой) и живой файл последним. Только существующие."""
    base = Path(path)
    parts: list[tuple[int, Path]] = []
    idx = 1
    while True:
        part = base.with_name(f"{base.name}.{idx}")
        if not part.exists():
            break
        parts.append((idx, part))
        idx += 1
    ordered = [p for _, p in sorted(parts, key=lambda pair: pair[0], reverse=True)]
    if base.exists():
        ordered.append(base)
    return ordered


def read_events_chain(path: str | Path) -> list[Event]:
    """IH-63: читает живой журнал и все ротированные части (`.1`, `.2`, ...)
    в хронологическом порядке: части — по убыванию индекса (`.2` старше
    `.1`), живой файл — последним. Битые строки внутри части —
    JournalCorrupt с именем части."""
    from io_core.errors import JournalCorrupt

    events: list[Event] = []
    for part_path in chain_files(path):
        with part_path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError as e:
                    raise JournalCorrupt(f"{part_path}:{n}: line is not valid JSON: {e}") from None
                if not isinstance(rec, dict):
                    raise JournalCorrupt(f"{part_path}:{n}: line is not a JSON object")
                events.append(rec)
    return events
