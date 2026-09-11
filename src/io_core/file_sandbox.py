"""Файловая песочница: доступ только внутри корня, квоты, журналирование (этап 1, задача 4).

Гарантии: resolve() разворачивает путь и требует, чтобы он остался внутри корня
(ловит и «..», и симлинки наружу); квоты считают текущее содержимое дерева.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from io_core.errors import QuotaExceeded, SandboxViolation

EventHook = Callable[[str, dict[str, Any]], None]


class FileSandbox:
    """File access confined to a root with byte/file-count quotas.

    Quota atomicity (IH-12): usage check + write happen under one lock, so
    parallel writers cannot both pass a quota check computed before either of
    them wrote (two 600 KB writes used to slip past a 1 MB limit together).
    """

    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int = 1_000_000,
        max_files: int = 100,
        on_event: EventHook | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._on_event = on_event
        self._lock = threading.Lock()

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, data)

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, rel_path: str) -> Path:
        if Path(rel_path).is_absolute():
            raise SandboxViolation(f"absolute path is not allowed: {rel_path!r}")
        resolved = (self._root / rel_path).resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise SandboxViolation(f"path {rel_path!r} escapes the sandbox root {self._root}")
        return resolved

    def _usage(self) -> tuple[int, int]:
        files = [p for p in self._root.rglob("*") if p.is_file()]
        return sum(f.stat().st_size for f in files), len(files)

    def _check_quota(self, extra_bytes: int, new_files: int) -> None:
        used_bytes, used_files = self._usage()
        if used_bytes + extra_bytes > self._max_bytes:
            raise QuotaExceeded(
                f"bytes: used {used_bytes}, adding {extra_bytes}, limit {self._max_bytes}"
            )
        if used_files + new_files > self._max_files:
            raise QuotaExceeded(
                f"files: used {used_files}, limit {self._max_files}"
            )

    def write_file(self, rel_path: str, data: bytes, *, overwrite: bool = False) -> int:
        # check + write under one lock: the quota scan is only meaningful if no
        # other write can land between it and our own write
        with self._lock:
            target = self.resolve(rel_path)
            if target.exists() and not overwrite:
                raise FileExistsError(rel_path)
            adding = len(data) - (target.stat().st_size if target.exists() else 0)
            self._check_quota(max(adding, 0), 0 if target.exists() else 1)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self._emit("file_write", {"path": rel_path, "bytes": len(data)})
        return len(data)

    def read_file(self, rel_path: str) -> bytes:
        data = self.resolve(rel_path).read_bytes()
        self._emit("file_read", {"path": rel_path, "bytes": len(data)})
        return data

    def list_dir(self, rel_path: str = ".") -> list[str]:
        target = self.resolve(rel_path)
        if not target.is_dir():
            raise NotADirectoryError(rel_path)
        entries = sorted(p.relative_to(self._root).as_posix() for p in target.rglob("*"))
        self._emit("file_list", {"path": rel_path, "count": len(entries)})
        return entries

    def delete_file(self, rel_path: str) -> None:
        with self._lock:
            target = self.resolve(rel_path)
            target.unlink()
        self._emit("file_delete", {"path": rel_path})
