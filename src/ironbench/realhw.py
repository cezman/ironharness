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
from typing import Any

from io_core.errors import TransportIoError
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
            chunk = data.decode("utf-8", "replace")
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

    def boot(self, code: str) -> None:
        """Гигиена + запуск entry: снести чужой main.py, soft reset, залить код.

        Буфер очищается перед staging (вместе с _chunks — античит отображает
        позиции текста на штампы чанков). Легаси paste-режим эхолит залитый
        исходник — после Ctrl+D буфер усекается по концу эха последней строки
        кода: эхо и баннер paste-режима в оценку и античит не попадают, а
        рантайм-вывод не теряется (исполнение начинается только после
        Ctrl+D, так что рантайм-вывод не может оказаться до конца эха).
        """
        self.interrupt()
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

        deadline = time.monotonic() + 8
        while PASTE_BANNER not in self.output():
            if time.monotonic() >= deadline:
                raise ConnectionError("board did not enter paste mode (Ctrl+E)")
            self.write(CTRL_E)
            self.wait_for(PASTE_BANNER, time.monotonic() + 2.0)
        for line in code.splitlines(keepends=True):
            self.write(line.encode("utf-8"))
        self.write(CTRL_D)  # выполнить; вывод читается в wait_for/дочитывании
        self._drain()
        self._truncate_after_staging_echo(code)

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
