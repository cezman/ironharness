"""Real-hardware target helper: MicroPython REPL over a serial link.

On ESP32 boards with a USB-UART bridge (CH340) the REPL and the application
UART are the same serial line, so a golden task runs against the live board
with unix-target semantics: the entry script is staged into the board's
filesystem and run, then the same stimulus steps (write-serial, wait-serial,
delay) drive the firmware and expect/fail patterns score the output.

CH340/USB-UART constraints learned on live hardware (IH-2):
- single-threaded IO only — a background reader thread racing a write
  segfaults the CH340 driver and bursts come back as NUL bytes;
- every write is rate-limited (small chunks + delays), bursts corrupt;
- the REPL line editor terminates input() on \\r only (\\n is silent) —
  stimulus writes are normalized to \\r by the runner;
- the entry is staged line-by-line in paste mode after the "paste mode"
  banner; legacy paste ECHOES the staged source back (live-verified IH-33,
  ESP32 MicroPython v1.27), so boot() truncates the buffer right after the
  echoed last source line - task patterns and the anti-cheat anchor must
  never see source literals
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from io_core.errors import TransportIoError
from io_core.incremental_text import Utf8StreamDecoder
from ironbench.runner_common import MAX_SERIAL_TEXT

CTRL_C = b"\x03"
CTRL_D = b"\x04"
CTRL_E = b"\x05"
PASTE_BANNER = "paste mode"
# MicroPython boots ~2.5 s after the port-open reset pulse; a soft reboot is faster
_BOOT_QUIET_SEC = 3.5
_SOFT_RESET_SEC = 2.0
_WRITE_CHUNK = 24
_WRITE_CHUNK_DELAY = 0.04
REMOVE_MAIN = b"import os; os.remove('main.py') if 'main.py' in os.listdir() else None\r\n"

# IH-79 backup probe, cooked-REPL safe. PAID LESSON (2026-09-20, live board):
# the MicroPython cooked REPL auto-indents after a colon line - a multi-line
# try/except block pasted here doubled/quadrupled the indentation until
# `except` sat inside the suite, the block never executed and the board sat
# in the line editor where Ctrl+D/Ctrl+E are dead: every staging died with
# "did not enter paste mode". The probe is therefore single-line conditional
# expressions with no colon at all (block opener), so each line executes
# immediately at the prompt. Markers stay split ('IH-BACK' + 'UP') - the
# cooked echo must never contain the marker contiguously.
_BACKUP_PROBE_LINES = (
    b"import binascii, os\r\n",
    (
        b"print('IH-BACK' + 'UP', binascii.hexlify(open('main.py', 'rb').read()).decode()) "
        b"if 'main.py' in os.listdir() else print('IH-BACK' + 'UP-ABSENT')\r\n"
    ),
)
# audit B: the probe waits on a sliding idle deadline - quiet window per chunk,
# bounded overall so a silent board cannot stall the staging
_PROBE_QUIET_SEC = 2.0
_PROBE_TOTAL_CAP = 10.0
_PROBE_MAX_BYTES = 256 * 1024


class RealRepl:
    """Drives a MicroPython REPL over an open serial transport.

    Single-threaded by design: writes are rate-limited chunks, reads happen
    only in _pump() between steps. No concurrent IO on the CH340 link.
    """

    def __init__(self, transport: Any, *, boot_quiet_sec: float | None = None) -> None:
        self._t = transport
        self._text = ""
        # every ingested chunk as (monotonic stamp, text), position-consistent
        # with _text (IH-33): the runner maps a needle's first occurrence back
        # to its chunk stamp for the anti-cheat anchor
        self._chunks: list[tuple[float, str]] = []
        self._truncated = False  # IH-46: the retention cap was hit
        self._dec = Utf8StreamDecoder()  # IH-61: holds a partial multibyte char across reads
        time.sleep(_BOOT_QUIET_SEC if boot_quiet_sec is None else boot_quiet_sec)
        self.interrupt()

    def _pump(self) -> None:
        """Забирает из транспорта всё, что пришло (без блокировки на холостом ходу).
        IH-46: удерживаемый текст ограничен MAX_SERIAL_TEXT — после переполнения
        чтение продолжается (ссылка остаётся чистой), в буфер попадает только
        маркер усечения; (text, chunks) остаются позиционно согласованными."""
        while True:
            in_waiting = getattr(self._t, "in_waiting", None)
            if in_waiting:
                data = self._t.read(int(in_waiting))
            elif in_waiting == 0:
                return
            else:
                data = self._t.read(256)  # фейки без in_waiting: read неблокирующий
            if not data:
                return
            if len(self._text) >= MAX_SERIAL_TEXT:
                if not self._truncated:
                    self._truncated = True
                    marker = (
                        f"\n[ironharness: serial output truncated at "
                        f"{MAX_SERIAL_TEXT} bytes - further output discarded]\n"
                    )
                    stamp = time.monotonic()
                    self._chunks.append((stamp, marker))
                    self._text += marker
                continue  # кап достигнут: дренируем, но не удерживаем
            chunk = self._dec.decode(data)  # IH-61: a partial char waits for the rest
            self._chunks.append((time.monotonic(), chunk))
            self._text += chunk

    def chunks(self) -> list[tuple[float, str]]:
        """A snapshot of the ingested (stamp, text) chunks (see _pump)."""
        return list(self._chunks)

    def output(self) -> str:
        self._pump()
        return self._text

    def write(self, data: bytes) -> None:
        for i in range(0, len(data), _WRITE_CHUNK):
            self._t.write(data[i : i + _WRITE_CHUNK])
            time.sleep(_WRITE_CHUNK_DELAY)

    def interrupt(self) -> None:
        """Ctrl+C x2: прервать работающий скрипт и оказаться на приглашении REPL."""
        self.write(CTRL_C + CTRL_C)
        time.sleep(0.4)
        self._drain()

    def _drain(self) -> None:
        for _ in range(6):
            before = len(self._text)
            self._pump()
            if len(self._text) == before:
                break
            time.sleep(0.05)

    def boot(self, code: str, *, deadline: float | None = None, backup_dir=None) -> str:
        """Гигиена + запуск entry: снести чужой main.py, soft reset, залить код.

        Буфер очищается перед staging (вместе с _chunks — античит отображает
        позиции текста на штампы чанков). Легаси paste-режим эхолит залитый
        исходник — после Ctrl+D буфер усекается по концу эха последней строки
        кода: эхо и баннер paste-режима в оценку и античит не попадают, а
        рантайм-вывод не теряется (исполнение начинается только после
        Ctrl+D, так что рантайм-вывод не может оказаться до конца эха).
        IH-79: перед сносом main.py сохраняется в backup_dir (файл
        main.py.backup) — main.py принадлежит пользователю, а не харнессу.
        Возвращает статус бэкапа: saved / absent / unknown / not requested.
        """
        self.interrupt()
        backup_status = "not requested"
        if backup_dir is not None:
            backup_status = self._backup_main(backup_dir)
            # audit B (2026-09-20): the probe could not read main.py and could
            # not prove it absent - wiping now could destroy user firmware we
            # never captured. Refuse before any destructive write.
            if backup_status == "unknown":
                raise ConnectionError(
                    "refusing to wipe main.py without a verified backup "
                    "(the backup probe got no parseable answer)"
                )
        self.write(REMOVE_MAIN)
        time.sleep(0.6)
        self._drain()
        self.write(CTRL_D)  # soft reset: чистое состояние без main.py
        time.sleep(_SOFT_RESET_SEC)
        self._drain()
        # text and chunks are cleared together - the anti-cheat maps positions
        # in text back to chunk stamps, so they must stay consistent
        self._text = ""
        self._chunks = []

        paste_deadline = time.monotonic() + 8
        while PASTE_BANNER not in self.output():
            if time.monotonic() >= paste_deadline:
                raise ConnectionError("board did not enter paste mode (Ctrl+E)")
            self.write(CTRL_E)
            self.wait_for(PASTE_BANNER, time.monotonic() + 2.0)
        for line in code.splitlines(keepends=True):
            if deadline is not None and time.monotonic() >= deadline:
                # IH-57: построчная заливка с фиксированными паузами без
                # дедлайна тянула попытку далеко за wall clock
                raise TimeoutError("staging exceeded the wall deadline (IH-57)")
            self.write(line.encode("utf-8"))
        self.write(CTRL_D)  # выполнить; вывод читается в wait_for/дочитывании
        self._drain()
        self._truncate_after_staging_echo(code)
        return backup_status

    def _backup_main(self, backup_dir) -> str:
        """IH-79: main.py принадлежит пользователю — перед сносом содержимое
        уходит в run-артефакты (main.py.backup). Читаем через cooked REPL в
        hex (binascii) — cooked-режим ест UTF-8, hex безопасен.

        Проба — однострочные выражения (_BACKUP_PROBE_LINES): cooked REPL
        автоиндентит после двоеточия, мультистрочный блок в нём не исполняется
        никогда (урок 2026-09-20 — живая плата, «did not enter paste mode»).
        Литералы маркеров разрезаны в исходнике пробы ('IH-BACK' + 'UP'):
        cooked-REPL эхолит каждый принятый байт, и неразрезанный литерал
        светился бы в собственном эхе раньше реального ответа (на эхоящей
        плате проба всегда давала бы «absent», а main.py стирался бы).
        Парсится только вывод ПОСЛЕ старта пробы — буфер может хранить
        вывод прошлой прошивки. Статус: saved / absent / unknown."""
        mark = len(self._text)
        for line in _BACKUP_PROBE_LINES:
            self.write(line)
            time.sleep(0.1)
        started = time.monotonic()
        deadline = started + _PROBE_QUIET_SEC
        # audit B: the answer trickles in at line rate - every new chunk
        # extends the window (sliding idle deadline), bounded by a total cap
        while time.monotonic() < deadline:
            time.sleep(0.2)
            before = len(self._text)
            self._drain()
            if len(self._text) > before and len(self._text) - mark < _PROBE_MAX_BYTES:
                deadline = min(time.monotonic() + _PROBE_QUIET_SEC, started + _PROBE_TOTAL_CAP)
            text = self._text[mark:]
            if "IH-BACKUP-ABSENT" in text:
                return "absent"
            idx = text.find("IH-BACKUP ")
            if idx >= 0:
                rest = text[idx + len("IH-BACKUP "):]
                nl = rest.find("\n")
                if nl < 0:
                    continue  # the answer line is still trickling in - wait
                try:
                    data = bytes.fromhex(rest[:nl].strip())
                except ValueError:
                    continue
                backup_dir = Path(backup_dir)
                backup_dir.mkdir(parents=True, exist_ok=True)
                (backup_dir / "main.py.backup").write_bytes(data)
                return "saved"
        return "unknown"

    def _truncate_after_staging_echo(self, code: str) -> None:
        """Срезает баннер paste-режима и эхо исходника: граница — конец эха
        последней непустой строки кода. rfind берёт последнее вхождение, то
        есть именно эхо финальной строки. Принятая граница: прошивка,
        печатающая при старте строку, совпадающую с последней строкой своего
        исходника, оставит хвост эха в логе (adversarial самосаботаж, не
        класс читеров).

        IH-56: «эха нет» и «эхо оборвалось по пути» — разные случаи. Если
        баннер paste-режима и первая строка исходника пришли, а последняя —
        нет, значит хвост эха потерян (FIFO/линия): неусечённое эхо осуждает
        честную плату или пропускает мёртвую — отказ вместо скоринга."""
        lines = [ln.rstrip() for ln in code.splitlines()]
        nonempty = [ln for ln in lines if ln]
        if not nonempty:
            return
        last = nonempty[-1]
        first = nonempty[0]
        pos = self._text.rfind(last)
        if pos >= 0:
            end = self._text.find("\n", pos)
            pos_end = len(self._text) if end < 0 else end + 1
            self._truncate_text_and_chunks(pos_end)
            return
        banner_pos = self._text.find(PASTE_BANNER)
        if banner_pos >= 0 and first in self._text[banner_pos:]:
            # эхо началось, но его хвост потерян: скорить такой лог нельзя
            raise TransportIoError(
                "staging echo truncated (FIFO/link lost the tail) - the run "
                "cannot be scored honestly; retry with a fresh paste"
            )
        # эха не было вовсе (raw-paste плата) — тримить нечего

    def _truncate_text_and_chunks(self, pos: int) -> None:
        """Drops everything before byte offset pos from text and chunks
        together (position consistency for the anti-cheat mapping)."""
        if pos <= 0:
            return
        self._text = self._text[pos:]
        kept: list[tuple[float, str]] = []
        seen = 0
        for stamp, chunk in self._chunks:
            chunk_end = seen + len(chunk)
            if chunk_end > pos:
                kept.append((stamp, chunk[max(0, pos - seen):]))
            seen = chunk_end
        self._chunks = kept

    def wait_for(self, needle: str, deadline: float) -> bool:
        """Ждёт подстроку в накопленном выводе до дедлайна; False — время вышло."""
        while time.monotonic() < deadline:
            if needle in self.output():
                return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        try:
            self._t.close()
        except (OSError, ValueError, AssertionError):
            pass
