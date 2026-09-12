"""MicroPython filesystem transfer over the raw REPL (IH-34).

serial_put / serial_get push and pull files over an open serial transport
using the raw REPL (Ctrl+A) - the same mechanism mpremote/pyboard use for
filesystem access. Chunk payloads travel as hex, decoded on the board with
binascii.unhexlify: every wire byte stays printable, which is the honest
primitive for a CH340 link (bursts corrupt, cooked modes mangle non-ASCII).
Paste mode is NOT used here: it echoes the source and has no per-chunk
error reporting, while each raw-REPL exec returns the board's traceback.

The driver assumes an idle MicroPython REPL: enter() interrupts running
firmware (Ctrl+C x2) and enters raw mode; exit() returns to the normal
REPL (no soft reset - the caller decides whether to reset the board).

Trust boundary (checklist item 3): the device path here is a path in the
BOARD's filesystem, not a host path - the host sandbox does not and cannot
apply to it. That grants no new capability: a caller with serial access can
already run arbitrary MicroPython through the same REPL, so board-side
writes are in-band payload, not a privilege escalation. Host-side safety
lives in the sandbox (for the source/destination files) and in the journal.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

CTRL_A = b"\x01"
CTRL_B = b"\x02"
CTRL_C = b"\x03"
CTRL_D = b"\x04"
PROMPT = b">"

DEFAULT_CHUNK = 256  # bytes per exec; hex-doubled on the wire (512 printable chars)
EXEC_TIMEOUT_SEC = 10.0
ENTER_TIMEOUT_SEC = 20.0  # whole enter budget: the board may be mid-boot (open reset pulse)
ENTER_ATTEMPT_SEC = 5.0  # one dance attempt; a timeout means "still booting", retry


class MpReplError(RuntimeError):
    """The board answered an exec with a traceback (syntax error, missing
    file, disk full...). The board's error text is carried verbatim."""


class _Stream:
    """Byte-accurate response reader: answers may arrive in one lump
    (output + \\x04 + error + \\x04 + '>'), so every section is cut from a
    persistent buffer instead of trusting read() chunking."""

    def __init__(self, t: Any) -> None:
        self._t = t
        self._buf = b""

    def _fill(self, deadline: float) -> None:
        while not self._buf:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"board did not answer within {EXEC_TIMEOUT_SEC}s (raw REPL)"
                )
            self._buf += self._t.read(256)

    def until(self, terminator: bytes, deadline: float) -> bytes:
        """Bytes before the next `terminator`; the terminator is consumed."""
        while terminator not in self._buf:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"board did not answer within {EXEC_TIMEOUT_SEC}s (raw REPL)"
                )
            self._buf += self._t.read(256)
        idx = self._buf.index(terminator)
        piece, self._buf = self._buf[:idx], self._buf[idx + len(terminator) :]
        return piece

    def until_prompt(self, deadline: float) -> bytes:
        return self.until(PROMPT, deadline)

    def quiet_read(self) -> bytes:
        """Whatever is left in the buffer (no waiting)."""
        chunk, self._buf = self._buf, b""
        return chunk


class MpRepl:
    """One raw-REPL session over a transport: enter, exec, put/get, exit."""

    def __init__(self, t: Any, *, chunk: int = DEFAULT_CHUNK) -> None:
        self._t = t
        self._chunk = chunk
        self._s = _Stream(t)

    def enter(self) -> None:
        """Reaches raw REPL from any board state, with retries.

        Opening a CH340 port pulses the reset line, so the board is often
        mid-boot when the transfer starts and the first dance is swallowed.
        Each attempt runs Ctrl+B (a board stuck in raw mode treats Ctrl+C as
        data - only Ctrl+B leaves; in normal mode it is a harmless banner),
        then the interrupt, then Ctrl+A, and waits for the raw-mode banner -
        not for the first '>', so boot junk containing prompts cannot
        satisfy it. A timeout means the board was still booting: retry.
        """
        deadline = time.monotonic() + ENTER_TIMEOUT_SEC
        while True:
            attempt_deadline = time.monotonic() + ENTER_ATTEMPT_SEC
            try:
                self._t.write(CTRL_B)
                time.sleep(0.1)
                self._t.write(CTRL_C + CTRL_C)
                time.sleep(0.3)
                self._t.write(CTRL_A)
                self._s.until(b"raw REPL", attempt_deadline)
                self._s.until_prompt(attempt_deadline)
                return
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1.0)  # let the boot finish, then dance again

    def exit_(self) -> None:
        """Back to the normal REPL. Best effort: a dead board must not mask
        the original error of a failed transfer."""
        with contextlib.suppress(OSError):
            self._t.write(CTRL_B)

    def exec_(self, command: str) -> str:
        """Runs one command in raw REPL; returns its printed output.

        The board answers with 'OK' + output + Ctrl+D + error text + Ctrl+D
        + '>'; the 'OK' acceptance mark is stripped here. A non-empty error
        text raises MpReplError carrying the traceback.
        """
        deadline = time.monotonic() + EXEC_TIMEOUT_SEC
        self._t.write(command.encode("utf-8") + CTRL_D)
        out = self._s.until(CTRL_D, deadline)
        err = self._s.until(CTRL_D, deadline)
        self._s.until_prompt(deadline)
        if out.startswith(b"OK"):
            out = out[2:]  # the raw-REPL acceptance mark, before any print output
        error = err.decode("utf-8", "replace").strip()
        if error:
            raise MpReplError(error)
        return out.decode("utf-8", "replace")

    def put_file(self, data: bytes, target_path: str) -> int:
        """Writes `data` to target_path on the board; returns the byte count.

        The file is opened 'wb' (an overwrite, not an append); a failure
        leaves a best-effort f.close() behind so the board is not left with
        an open handle.
        """
        self.enter()
        try:
            self.exec_("import binascii")
            self.exec_(f"f = open({target_path!r}, 'wb')")
            try:
                for i in range(0, len(data), self._chunk):
                    piece = data[i : i + self._chunk].hex()
                    self.exec_(f"f.write(binascii.unhexlify('{piece}'))")
                self.exec_("f.close()")
            except BaseException:
                self._best_effort_close()
                raise
        except BaseException:
            self.exit_()
            raise
        self.exit_()
        return len(data)

    def get_file(self, target_path: str) -> bytes:
        """Reads target_path from the board and returns its bytes.

        The board prints each chunk hex-encoded; the terminating b'' marks
        EOF. A missing file surfaces as the board's ENOENT traceback in
        MpReplError.
        """
        self.enter()
        try:
            self.exec_("import binascii")
            self.exec_(f"f = open({target_path!r}, 'rb')")
            try:
                parts: list[bytes] = []
                while True:
                    out = self.exec_(f"print(binascii.hexlify(f.read({self._chunk})))").strip()
                    if out in ("", "b''"):
                        break
                    if not (out.startswith("b'") and out.endswith("'")):
                        raise MpReplError(f"unexpected board answer: {out!r}")
                    parts.append(bytes.fromhex(out[2:-1]))
                self.exec_("f.close()")
            except BaseException:
                self._best_effort_close()
                raise
        except BaseException:
            self.exit_()
            raise
        self.exit_()
        return b"".join(parts)

    def _best_effort_close(self) -> None:
        """f.close() after a failed transfer - errors here (dead board, dead
        link) must never mask the original transfer failure."""
        with contextlib.suppress(Exception):
            self.exec_("try:\n    f.close()\nexcept NameError:\n    pass")


def put_file(t: Any, data: bytes, target_path: str, *, chunk: int = DEFAULT_CHUNK) -> int:
    """Module-level entry: pushes `data` to target_path over raw REPL."""
    return MpRepl(t, chunk=chunk).put_file(data, target_path)


def get_file(t: Any, target_path: str, *, chunk: int = DEFAULT_CHUNK) -> bytes:
    """Module-level entry: pulls target_path from the board over raw REPL."""
    return MpRepl(t, chunk=chunk).get_file(target_path)
