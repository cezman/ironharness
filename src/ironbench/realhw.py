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
  banner; boot() clears the output buffer after staging so pattern scoring
  only sees the run itself.
"""

from __future__ import annotations

import time
from typing import Any

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
        time.sleep(_BOOT_QUIET_SEC if boot_quiet_sec is None else boot_quiet_sec)
        self.interrupt()

    def _pump(self) -> None:
        """Drains everything that arrived from the transport (without blocking when idle)."""
        while True:
            in_waiting = getattr(self._t, "in_waiting", None)
            if in_waiting:
                data = self._t.read(int(in_waiting))
            elif in_waiting == 0:
                return
            else:
                data = self._t.read(256)  # fakes without in_waiting: read is non-blocking
            if not data:
                return
            self._text += data.decode("utf-8", "replace")

    def output(self) -> str:
        self._pump()
        return self._text

    def write(self, data: bytes) -> None:
        for i in range(0, len(data), _WRITE_CHUNK):
            self._t.write(data[i : i + _WRITE_CHUNK])
            time.sleep(_WRITE_CHUNK_DELAY)

    def interrupt(self) -> None:
        """Ctrl+C x2: abort a running script and land on the REPL prompt."""
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
        """Hygiene + entry run: remove a foreign main.py, soft reset, paste the code.

        After staging the output buffer is cleared - only the run itself counts for
        scoring (a Traceback from interrupting foreign firmware or boot noise does
        not fail the task).
        """
        self.interrupt()
        self.write(REMOVE_MAIN)
        time.sleep(0.6)
        self._drain()
        self.write(CTRL_D)  # soft reset: a clean state without main.py
        time.sleep(_SOFT_RESET_SEC)
        self._drain()
        self._text = ""

        deadline = time.monotonic() + 8
        while PASTE_BANNER not in self.output():
            if time.monotonic() >= deadline:
                raise ConnectionError("board did not enter paste mode (Ctrl+E)")
            self.write(CTRL_E)
            self.wait_for(PASTE_BANNER, time.monotonic() + 2.0)
        for line in code.splitlines(keepends=True):
            self.write(line.encode("utf-8"))
        self.write(CTRL_D)  # execute; the output is read in wait_for/keep-reading

    def wait_for(self, needle: str, deadline: float) -> bool:
        """Waits for a substring in the accumulated output until the deadline; False - time is up."""
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
