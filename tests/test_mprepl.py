"""Тесты файлового трансфера через raw REPL (IH-34). Железа нет: FakeRawBoard
разбирает команды драйвера структурно и исполняет их семантику по-настоящему
(unhexlify = bytes.fromhex, режимы wb/rb, traceback при ошибке), так что
раунд-трип проверяет сам протокол, а не эхо заготовленных строк. Тихие отказы
serial_write (класс queue.Full / плохой hex) закрыты отдельными тестами."""

import ast
import queue
import re
import threading

import pytest

from io_core import Session
from io_core.errors import TransportIoError
from io_core.journal import read_events
from io_core.mprepl import MpReplError, get_file, put_file

_RE_IMPORT = re.compile(r"^import binascii$")
_RE_OPEN = re.compile(r"^f = open\((.+), '([rwa]b?)'\)$")
_RE_WRITE = re.compile(r"^f\.write\(binascii\.unhexlify\('([0-9a-f]*)'\)\)$")
_RE_CLOSE = re.compile(r"^f\.close\(\)$")
_RE_READ = re.compile(r"^print\(binascii\.hexlify\(f\.read\((\d+)\)\)\)$")


class FakeRawBoard:
    """A MicroPython raw REPL over a fake filesystem: the driver's exec lines
    are matched against the exact command shapes mprepl.py emits and their
    semantics run for real (hex -> bytes, position-tracking reads, ENOENT).
    Nothing is echoed back blindly - the driver's bytes survive the round trip.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytearray] = {}
        self._inbuf = bytearray()
        self._outbuf = bytearray()
        self._raw = False
        self._f: tuple[str, str, int, int] | None = None  # path, mode, wpos, rpos

    def _emit(self, data: bytes) -> None:
        self._outbuf += data

    def _fail(self, message: str) -> None:
        self._f = None  # the board drops the handle on an error
        self._emit(b"\x04" + f"Traceback (most recent call last):\r\n{message}\r\n".encode() + b"\x04>")

    def _exec_line(self, command: str) -> None:
        out: list[bytes] = []
        try:
            if _RE_IMPORT.match(command):
                pass  # the stub module is baked into the read/write handlers
            elif m := _RE_OPEN.match(command):
                path = ast.literal_eval(m.group(1))
                mode = m.group(2)
                if "r" in mode and path not in self.files:
                    raise OSError(2, "ENOENT")
                if "w" in mode:
                    self.files[path] = bytearray()  # 'wb' truncates
                self._f = (path, mode, len(self.files[path]), 0)
            elif m := _RE_WRITE.match(command):
                if self._f is None:
                    raise OSError(9, "file is not open")
                path, _mode, wpos, rpos = self._f
                piece = bytes.fromhex(m.group(1))
                buf = self.files.setdefault(path, bytearray())
                end = wpos + len(piece)
                if end > len(buf):
                    buf.extend(b"\x00" * (end - len(buf)))
                buf[wpos:end] = piece
                self._f = (path, _mode, end, rpos)
            elif m := _RE_READ.match(command):
                if self._f is None:
                    raise OSError(9, "file is not open")
                path, _mode, wpos, rpos = self._f
                size = int(m.group(1))
                chunk = bytes(self.files.get(path, bytearray())[rpos : rpos + size])
                self._f = (path, _mode, wpos, rpos + len(chunk))
                out.append(("b'" + chunk.hex() + "'").encode() + b"\r\n")
            elif _RE_CLOSE.match(command):
                self._f = None
            else:
                raise SyntaxError(f"invalid command: {command!r}")
        except Exception as e:  # noqa: BLE001 - the board answers with a traceback section
            self._fail(f"{type(e).__name__}: {e}")
            return
        self._emit(b"OK" + b"".join(out) + b"\x04\x04>")

    def write(self, data: bytes) -> int:
        for byte in data:
            self._inbuf.append(byte)
            if not self._raw:
                if self._inbuf[-1:] == b"\x01":  # Ctrl+A: enter raw mode
                    self._inbuf.clear()
                    self._raw = True
                    self._emit(b"raw REPL; CTRL-B to exit\r\n>")
            elif self._inbuf[-1:] == b"\x02":  # Ctrl+B: back to the normal REPL
                self._inbuf.clear()
                self._raw = False
                self._emit(b"\r\n>")
            elif self._inbuf[-1:] == b"\x04":  # Ctrl+D: execute the buffered command
                command = bytes(self._inbuf[:-1]).decode("utf-8", "replace")
                self._inbuf.clear()
                if command.strip():
                    self._exec_line(command.strip("\r\n"))
                else:
                    self._emit(b"\x04\x04>")
        return len(data)

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self._outbuf[:size])
        del self._outbuf[:size]
        return chunk

    def close(self) -> None:
        self.closed = True


def test_put_and_get_round_trip():
    board = FakeRawBoard()
    payload = (
        b"# micro python firmware\n"
        b"print('hello')\n" * 40  # 3 chunks of 256 bytes: exercises the loop
    )
    n = put_file(board, payload, "/main.py")
    assert n == len(payload)
    assert bytes(board.files["/main.py"]) == payload
    back = get_file(board, "/main.py")
    assert back == payload


def test_put_overwrites_target():
    board = FakeRawBoard()
    put_file(board, b"first", "/f.py")
    put_file(board, b"second", "/f.py")
    assert bytes(board.files["/f.py"]) == b"second"


def test_enter_recovers_from_stuck_raw_mode():
    # live finding (IH-34): a board left in raw REPL treats Ctrl+C as data -
    # enter() must leave raw mode first (Ctrl+B) or it waits forever
    board = FakeRawBoard()
    board._raw = True  # e.g. a previous transfer died mid-session
    put_file(board, b"payload", "/f.py")
    assert bytes(board.files["/f.py"]) == b"payload"


def test_get_missing_file_is_mp_repl_error():
    board = FakeRawBoard()
    with pytest.raises(MpReplError, match="ENOENT"):
        get_file(board, "/nope.py")


def test_board_error_mid_transfer_propagates():
    # the board refuses a write (read-only media...): the driver surfaces the
    # board's traceback, not an anonymous tool error
    class _ReadOnly(FakeRawBoard):
        def _exec_line(self, command: str) -> None:
            if "f.write" in command:
                self._f = None
                self._emit(b"\x04" + b"OSError: read-only media\r\n" + b"\x04>")
                return
            super()._exec_line(command)

    ro = _ReadOnly()
    with pytest.raises(MpReplError, match="read-only media"):
        put_file(ro, b"x" * 600, "/f.py")
    assert not ro.files.get("/f.py")


def test_binary_payload_survives_round_trip():
    board = FakeRawBoard()
    payload = bytes(range(256)) * 2  # non-text bytes: the hex encoding must carry them
    put_file(board, payload, "/blob.bin")
    assert get_file(board, "/blob.bin") == payload


# --- the silent serial_write failure class (IH-34) ---


class _OverflowSerial:
    """Duck-typed pyserial handle: write overflows its buffer with queue.Full -
    NOT an OSError - exactly what live loop:// does past its buffer size."""

    def write(self, data: bytes) -> int:
        raise queue.Full()

    def read(self, size: int = 1) -> bytes:
        return b""

    def close(self) -> None:
        pass


class _StubReader:
    """Just enough of SerialReader for the session-close path: the put/get
    guard only checks the readers dict for membership."""

    def __init__(self) -> None:
        self.io_lock = threading.Lock()

    def stats(self) -> dict[str, int]:
        return {"buffered": 0, "dropped": 0, "chunks": 0}

    def stop(self) -> dict[str, int]:
        return self.stats()


def test_serial_write_buffer_overflow_is_journaled(tmp_path):
    from io_core.serial_transport import SerialTransport

    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    transport = SerialTransport("loop://", on_event=lambda kind, data: events.append((kind, data)))
    transport._serial = _OverflowSerial()  # the opened handle, replaced for the test
    events: list = []
    s._transports["s"] = transport  # bypass serial_open: the handle is the subject
    s._kinds["s"] = "serial"
    try:
        with pytest.raises(TransportIoError):  # an OSError subclass: handlers keep catching
            s.serial_write("s", "41" * 5000)
    finally:
        s.close()
    kinds = [kind for kind, _ in events]
    assert "write_failed" in kinds
    assert any("queue.Full" in str(data.get("error")) for _, data in events if _ == "write_failed")


def test_serial_write_bad_hex_is_journaled(tmp_path):
    # the bench's original silent failure: a malformed hex argument died in
    # bytes.fromhex BEFORE any journaling and surfaced as a bare tool error
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    s._transports["s"] = _OverflowSerial()
    s._kinds["s"] = "serial"
    try:
        with pytest.raises(ValueError, match="not valid hex"):
            s.serial_write("s", "zz-not-hex")
    finally:
        s.close()
    events = read_events(tmp_path / "journal.jsonl")
    failed = [e for e in events if e["kind"] == "write_failed"]
    assert failed and "not valid hex" in failed[0]["error"]


# --- session-level put/get against the fake board ---


def test_session_serial_put_get_round_trip(tmp_path):
    # sandbox file -> board -> sandbox file, with the journal carrying the fact
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    board = FakeRawBoard()
    s._transports["b"] = board
    s._kinds["b"] = "serial"
    try:
        s.file_write("staged/main.py", "print('firmware')\n")
        n = s.serial_put("b", "staged/main.py", "/main.py")
        assert n == len(b"print('firmware')\n")
        assert bytes(board.files["/main.py"]) == b"print('firmware')\n"
        got = s.serial_get("b", "/main.py", "pulled/main.py")
        assert got == len(b"print('firmware')\n")
        assert s.file_read("pulled/main.py") == "print('firmware')\n"
    finally:
        s.close()
    kinds = [e["kind"] for e in read_events(tmp_path / "journal.jsonl")]
    assert "serial_put" in kinds and "serial_get" in kinds


def test_session_serial_put_refuses_under_reader(tmp_path):
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    s._transports["b"] = FakeRawBoard()
    s._kinds["b"] = "serial"
    s.file_write("staged/main.py", "print('firmware')\n")  # the source must exist:
    # the reader guard fires before the transfer, not before the staging read
    s._readers["b"] = _StubReader()
    try:
        with pytest.raises(RuntimeError, match="background reader"):
            s.serial_put("b", "staged/main.py", "/main.py")
        with pytest.raises(RuntimeError, match="background reader"):
            s.serial_get("b", "/main.py", "pulled/main.py")
    finally:
        s.close()


def test_session_serial_failures_are_journaled(tmp_path):
    s = Session(tmp_path / "journal.jsonl", tmp_path / "sandbox", actor="test")
    board = FakeRawBoard()
    s._transports["b"] = board
    s._kinds["b"] = "serial"
    with pytest.raises(Exception):  # noqa: B017 - sandbox raises its own type
        s.serial_put("b", "missing/main.py", "/main.py")  # no such sandbox source
    with pytest.raises(MpReplError):
        s.serial_get("b", "/nope.py", "pulled/x.py")  # no such board file
    s.close()
    kinds = [e["kind"] for e in read_events(tmp_path / "journal.jsonl")]
    assert "serial_get_failed" in kinds
