"""Реплеер: воспроизводит ответы записанной сессии без железа (этап 1, задача 2).

Все read-события журнала склеиваются в непрерывный поток байт — реплей не
ломается от другой нарезки чтений (записали read(2)+read(2), воспроизводим
read(4)). Write-события сверяются с записью: в strict-режиме несовпадение
бросает ReplayMismatch, в lenient-режиме записи игнорируются.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from io_core.journal import Event, read_events


class ReplayMismatch(Exception):
    """Воспроизводимые write-данные не совпали с записанными."""


class ReplayTransport:
    def __init__(self, events: list[Event], *, strict: bool = True) -> None:
        self._strict = strict
        self._read_stream = bytearray()
        self._write_stream = bytearray()
        self._rpos = 0
        self._wpos = 0
        for e in events:
            if e.get("kind") in ("read", "read_line"):
                self._read_stream += bytes.fromhex(e["data_hex"])
            elif e.get("kind") == "write":
                self._write_stream += bytes.fromhex(e["data_hex"])

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: object) -> ReplayTransport:
        return cls(read_events(path), **kwargs)  # type: ignore[arg-type]

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
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
