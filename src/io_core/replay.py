"""Реплеер: воспроизводит ответы записанной сессии без железа (этап 1, задача 2).

С именованными соединениями (IH-11, 2026-09-09) журнал хранит "conn" в каждом
событии транспорта, и реплей ведётся ПО СОЕДИНЕНИЯМ: ReplaySession группирует
события по conn и даёт каждому соединению свой ReplayTransport со своим потоком
чтений и strict-сверкой записей. Раньше все чтения склеивались в один поток —
ответ второго устройства можно было прочитать из первого, а принадлежность
операций соединениям терялась. Порядок внутри соединения — порядок журнала;
ts записан в каждом событии, но при реплее не воспроизводится (реплей
детерминирован и мгновенен).

Журналы без "conn" (записанные транспортами напрямую или до IH-11) попадают в
одно безымянное соединение "" — старое поведение «всё в один поток».

Все read-события соединения склеиваются в непрерывный поток байт — реплей не
ломается от другой нарезки чтений (записали read(2)+read(2), воспроизводим
read(4)). Write-события сверяются с записью: в strict-режиме несовпадение
бросает ReplayMismatch, в lenient-режиме записи игнорируются.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from io_core.journal import Event, read_events

LEGACY_CONN = ""  # conn для событий без имени (журналы до IH-11 и «голые» транспорты)


class ReplayMismatch(Exception):
    """Воспроизводимые write-данные не совпали с записанными."""


class ReplayTransport:
    """Реплей ОДНОГО соединения: события фильтруются по conn (по умолчанию
    LEGACY_CONN — все безымянные события старых журналов). Для сессии с
    несколькими соединениями стройте транспорт на соединение через
    ReplaySession, иначе события других соединений не попадут в поток."""

    def __init__(self, events: list[Event], *, strict: bool = True, conn: str = LEGACY_CONN) -> None:
        self._strict = strict
        self.conn = conn
        self._read_stream = bytearray()
        self._write_stream = bytearray()
        self._rpos = 0
        self._wpos = 0
        for e in events:
            if str(e.get("conn", LEGACY_CONN)) != conn:
                continue
            if e.get("kind") in ("read", "read_line"):
                self._read_stream += bytes.fromhex(e["data_hex"])
            elif e.get("kind") == "write":
                self._write_stream += bytes.fromhex(e["data_hex"])

    @classmethod
    def from_file(
        cls, path: str | Path, *, conn: str = LEGACY_CONN, **kwargs: object
    ) -> ReplayTransport:
        return cls(read_events(path), conn=conn, **kwargs)  # type: ignore[arg-type]

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self._read_stream[self._rpos : self._rpos + size])
        self._rpos += len(chunk)
        return chunk

    def read_line(self, max_len: int = 256) -> bytes:
        end = self._read_stream.find(b"\n", self._rpos, self._rpos + max_len)
        stop = self._rpos + max_len if end == -1 else end + 1
        chunk = bytes(self._read_stream[self._rpos : stop])
        self._rpos += len(chunk)
        return chunk

    def write(self, data: bytes) -> int:
        if self._strict:
            recorded = bytes(self._write_stream[self._wpos : self._wpos + len(data)])
            if data != recorded:
                raise ReplayMismatch(
                    f"позиция {self._wpos}: записано {recorded.hex() or '<пусто>'}, "
                    f"воспроизводится {data.hex()}"
                )
        self._wpos += len(data)
        return len(data)


class ReplaySession:
    """По-соединений реплей записанной сессии: по одному ReplayTransport на
    каждое имя conn в порядке первого появления в журнале. Для сессии из двух
    устройств «a» и «b» ответы «b» больше не читаются из транспорта «a», и
    strict-сверка записей идёт отдельно на каждое соединение.

    Несколько open/close циклов одного имени — один непрерывный поток этого
    conn: реплеить можно только всю историю имени целиком, частичный реплей
    отдельной инкарнации требует ручной нарезки событий.
    """

    def __init__(self, events: list[Event], *, strict: bool = True) -> None:
        groups: dict[str, list[Event]] = {}
        order: list[str] = []
        for e in events:
            # str(): journal is untrusted input, a non-scalar conn must not crash
            conn = str(e.get("conn", LEGACY_CONN))
            if conn not in groups:
                groups[conn] = []
                order.append(conn)
            groups[conn].append(e)
        self.order = tuple(order)
        self.conns = {
            conn: ReplayTransport(group, strict=strict, conn=conn)
            for conn, group in groups.items()
        }

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: object) -> ReplaySession:
        return cls(read_events(path), **kwargs)  # type: ignore[arg-type]

    def __getitem__(self, name: str) -> ReplayTransport:
        return self.conns[name]

    def __contains__(self, name: str) -> bool:
        return name in self.conns
