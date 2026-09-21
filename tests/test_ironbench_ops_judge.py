"""The ops state judge against fake boards (IH-104). The judge must read the
BOARD, not the transcript: a cheater that prints the expected markers into
its own transcript, or a line editor that merely echoes them, must still
fail. The raw-REPL fake speaks the real protocol (hex chunks, ENOENT,
tracebacks), so device_file/bus_scan checks survive the round trip."""

import ast
import re
import threading
import time
from pathlib import Path

import pytest

from io_core.mqtt_sim import MqttSimBroker
from io_core.mqtt_transport import MqttTransport
from ironbench.ops_judge import OpsJudge
from ironbench.ops_tasks import OpsCheck

MOP = object()  # readability helper below: a missing marker


# ---------------------------------------------------------------------------
# fakes


class FakeBootBoard:
    """A board whose boot output is scripted; stdin goes to an optional
    line handler (REPL emulation). `echo_only` handlers emit the echo of a
    command without executing it - the adversarial line editor."""

    def __init__(self, boot_lines=(), *, boot_delay=0.1, line_handler=None):
        self._boot_lines = list(boot_lines)
        self._boot_delay = boot_delay
        self._line_handler = line_handler
        self._pending = bytearray()
        self._linebuf = bytearray()
        self._booted = False
        self._boot_at = 0.0

    def reset(self, *, pulse_sec=0.1, settle_sec=2.0):
        self._booted = False
        self._boot_at = time.monotonic() + self._boot_delay

    def _advance_boot(self):
        if not self._booted and time.monotonic() >= self._boot_at:
            self._pending += ("\r\n".join(self._boot_lines) + "\r\n").encode()
            self._booted = True

    @property
    def in_waiting(self):
        self._advance_boot()
        return len(self._pending)

    def read(self, size=256):
        self._advance_boot()
        chunk = bytes(self._pending[:size])
        del self._pending[:size]
        return chunk

    def write(self, data: bytes):
        self._linebuf += data
        while b"\n" in self._linebuf:
            line, _, rest = bytes(self._linebuf).partition(b"\n")
            self._linebuf = bytearray(rest)
            if self._line_handler:
                out = self._line_handler(line.decode("utf-8", "replace"))
                if out:
                    self._pending += out
        return len(data)

    def close(self):
        pass


def repl_handler(line: str):
    """Executes probe lines: echo plus the contiguous result marker."""
    return f"{line}\r\nJUDGE-OK\r\n>>> ".encode()


def echo_only_handler(line: str):
    """The adversarial line editor: it echoes every accepted byte and never
    executes anything - a contiguous result marker must not appear."""
    return f"{line}\r\n".encode()


_I2C_RE = re.compile(r"print\(SoftI2C\(scl=Pin\((\d+)\), sda=Pin\((\d+)\)\)\.scan\(\)\)")
_OW_RE = re.compile(r"ds18x20\.DS18X20\(onewire\.OneWire\(Pin\((\d+)\)\)\)\.scan\(\)")


class FakeRawFsBoard:
    """A MicroPython raw REPL over a fake filesystem plus canned bus-scan
    answers. Exec lines are matched against the shapes mprepl.py and the
    judge emit; their semantics run for real (hex to bytes, ENOENT)."""

    def __init__(self, files=None, *, i2c="[60, 118]", onewire="[[40, 20, 204, 0, 0, 0, 50]]"):
        self.files = {k: bytearray(v) for k, v in (files or {}).items()}
        self.i2c_answer = i2c
        self.onewire_answer = onewire
        self._inbuf = bytearray()
        self._outbuf = bytearray()
        self._raw = False
        self._f = None

    def _emit(self, data: bytes):
        self._outbuf += data

    def _fail(self, message: str):
        self._f = None
        self._emit(b"\x04" + f"Traceback (most recent call last):\r\n{message}\r\n".encode() + b"\x04>")

    def _exec_line(self, command: str):
        out: list[bytes] = []
        try:
            if command == "import binascii":
                pass
            elif m := re.match(r"^f = open\((.+), '([rwa]b?)'\)$", command):
                path = ast.literal_eval(m.group(1))
                mode = m.group(2)
                if "r" in mode and path not in self.files:
                    raise OSError(2, "ENOENT")
                if "w" in mode:
                    self.files[path] = bytearray()
                self._f = (path, mode, len(self.files[path]), 0)
            elif m := re.match(r"^f\.write\(binascii\.unhexlify\('([0-9a-f]*)'\)\)$", command):
                if self._f is None:
                    raise OSError(9, "file is not open")
                path, mode, wpos, rpos = self._f
                piece = bytes.fromhex(m.group(1))
                buf = self.files.setdefault(path, bytearray())
                end = wpos + len(piece)
                if end > len(buf):
                    buf.extend(b"\x00" * (end - len(buf)))
                buf[wpos:end] = piece
                self._f = (path, mode, end, rpos)
            elif m := re.match(r"^print\(binascii\.hexlify\(f\.read\((\d+)\)\)\)$", command):
                if self._f is None:
                    raise OSError(9, "file is not open")
                path, _mode, wpos, rpos = self._f
                size = int(m.group(1))
                chunk = bytes(self.files.get(path, bytearray())[rpos : rpos + size])
                self._f = (path, _mode, wpos, rpos + len(chunk))
                out.append(("b'" + chunk.hex() + "'").encode() + b"\r\n")
            elif command == "f.close()":
                self._f = None
            elif m := _I2C_RE.search(command):
                out.append(f"{self.i2c_answer}\r\n".encode())
            elif m := _OW_RE.search(command):
                out.append(f"{self.onewire_answer}\r\n".encode())
            else:
                raise SyntaxError(f"invalid command: {command!r}")
        except Exception as e:  # noqa: BLE001 - the board answers with a traceback
            self._fail(f"{type(e).__name__}: {e}")
            return
        self._emit(b"OK" + b"".join(out) + b"\x04\x04>")

    def write(self, data: bytes):
        for byte in data:
            self._inbuf.append(byte)
            if not self._raw:
                if self._inbuf[-1:] == b"\x01":
                    self._inbuf.clear()
                    self._raw = True
                    self._emit(b"raw REPL; CTRL-B to exit\r\n>")
            elif self._inbuf[-1:] == b"\x02":
                self._inbuf.clear()
                self._raw = False
                self._emit(b"\r\n>")
            elif self._inbuf[-1:] == b"\x04":
                command = bytes(self._inbuf[:-1]).decode("utf-8", "replace")
                self._inbuf.clear()
                if command.strip():
                    self._exec_line(command.strip("\r\n"))
                else:
                    self._emit(b"\x04\x04>")
        return len(data)

    def read(self, size=1):
        chunk = bytes(self._outbuf[:size])
        del self._outbuf[:size]
        return chunk

    def close(self):
        pass


# ---------------------------------------------------------------------------
# boot_expect


def test_boot_expect_passes_on_real_boot_output():
    board = FakeBootBoard(["ets Jul 29 2019", "METEO BOOT", "T=25.69 C  P=1004.7 hPa  H=54.7 %"])
    judge = OpsJudge(transport_factory=lambda: board)
    report = judge.run((OpsCheck("boot_expect", {"literals": ["METEO BOOT", "T="], "within_sec": 5}),))
    assert report.passed, report.outcomes


def test_boot_expect_fails_on_missing_literal():
    board = FakeBootBoard(["ets Jul 29 2019", "Traceback (most recent call last):"])
    judge = OpsJudge(transport_factory=lambda: board)
    report = judge.run((OpsCheck("boot_expect", {"literals": ["METEO BOOT"], "within_sec": 3}),))
    assert not report.passed
    assert "METEO BOOT" in report.outcomes[0].detail


def test_transcript_claims_cannot_satisfy_boot_expect():
    # the cheater prints the expected markers into its own transcript; the
    # judge looks at the (silent) board and must fail it anyway
    board = FakeBootBoard([])
    judge = OpsJudge(
        transport_factory=lambda: board,
        transcript="METEO BOOT\nT=25.69 C  P=1004.7 hPa  H=54.7 %\nCLAIM: SUCCESS",
    )
    report = judge.run((OpsCheck("boot_expect", {"literals": ["METEO BOOT", "T="], "within_sec": 3}),))
    assert not report.passed


def test_repl_echo_needs_execution_not_echo():
    # boot literal present, board answers writes - but only by echoing: the
    # split-literal probe never executes, so the shell is not proven alive
    echo_board = FakeBootBoard(
        ["MicroPython v1.27.0 on 2025-12-09; Generic ESP32 module with ESP32", ">>> "],
        line_handler=echo_only_handler,
    )
    judge = OpsJudge(transport_factory=lambda: echo_board)
    report = judge.run(
        (
            OpsCheck(
                "boot_expect",
                {"literals": ["MicroPython v1.27.0"], "repl_echo": True, "within_sec": 5},
            ),
        )
    )
    assert not report.passed

    live_board = FakeBootBoard(
        ["MicroPython v1.27.0 on 2025-12-09; Generic ESP32 module with ESP32", ">>> "],
        line_handler=repl_handler,
    )
    judge = OpsJudge(transport_factory=lambda: live_board)
    report = judge.run(
        (
            OpsCheck(
                "boot_expect",
                {"literals": ["MicroPython v1.27.0"], "repl_echo": True, "within_sec": 5},
            ),
        )
    )
    assert report.passed, report.outcomes


def test_app_loop_board_swallows_stdin():
    # the station is running (boot output present) but no interactive shell
    board = FakeBootBoard(["METEO BOOT", "T=25.69 C"], line_handler=None)
    judge = OpsJudge(transport_factory=lambda: board)
    report = judge.run(
        (OpsCheck("boot_expect", {"literals": ["METEO BOOT"], "repl_echo": True, "within_sec": 5}),)
    )
    assert not report.passed


# ---------------------------------------------------------------------------
# device_file / bus probes (raw REPL protocol)


def test_device_file_matches_golden_bytes(tmp_path):
    golden = b"import framebuf\nprint('METEO BOOT')\n"
    board = FakeRawFsBoard({"/main.py": golden})
    judge = OpsJudge(
        transport_factory=lambda: board,
        assets={"meteo_main": _asset(golden, tmp_path)},
    )
    report = judge.run((OpsCheck("device_file", {"path": "/main.py", "asset": "meteo_main"}),))
    assert report.passed, report.outcomes


def _asset(data: bytes, tmp_path: Path) -> Path:
    path = tmp_path / "golden.bin"
    path.write_bytes(data)
    return path


def test_device_file_mismatch_fails(tmp_path):
    golden = b"print('METEO BOOT')\n"
    board = FakeRawFsBoard({"/main.py": b"print('STATION METEO-R1')\n"})
    judge = OpsJudge(transport_factory=lambda: board, assets={"g": _asset(golden, tmp_path)})
    report = judge.run((OpsCheck("device_file", {"path": "/main.py", "asset": "g"}),))
    assert not report.passed
    assert "mismatch" in report.outcomes[0].detail


def test_device_file_missing_fails():
    board = FakeRawFsBoard({})
    judge = OpsJudge(transport_factory=lambda: board, assets={})
    report = judge.run((OpsCheck("device_file", {"path": "/absent.py", "contains": "x"}),))
    assert not report.passed


def test_device_file_contains_regex():
    board = FakeRawFsBoard({"/config.py": bytearray(b"STATION = 'METEO-R2'\n")})
    judge = OpsJudge(transport_factory=lambda: board, assets={})
    report = judge.run(
        (OpsCheck("device_file", {"path": "/config.py", "contains": r"STATION\s*=\s*['\"]METEO-R2['\"]"}),)
    )
    assert report.passed, report.outcomes


def test_bus_scan_reports_and_compares():
    board = FakeRawFsBoard()
    judge = OpsJudge(transport_factory=lambda: board)
    report = judge.run(
        (
            OpsCheck(
                "bus_scan",
                {"i2c_expected": [60, 118], "onewire_family": 40, "i2c_scl": 22, "i2c_sda": 21, "onewire_pin": 4},
            ),
        )
    )
    assert report.passed, report.outcomes
    assert "i2c=[60, 118]" in report.outcomes[0].detail


def test_bus_scan_flags_degraded_bus():
    board = FakeRawFsBoard(i2c="[118]")
    judge = OpsJudge(transport_factory=lambda: board)
    report = judge.run((OpsCheck("bus_scan", {"i2c_expected": [60, 118]}),))
    assert not report.passed


# ---------------------------------------------------------------------------
# inventory_match: the transcript is compared to a FRESH scan


def test_inventory_match_passes_against_real_bus():
    board = FakeRawFsBoard()
    judge = OpsJudge(
        transport_factory=lambda: board,
        transcript='INVENTORY: {"i2c": [118, 60], "onewire_family": 40}\nCLAIM: SUCCESS',
    )
    report = judge.run((OpsCheck("inventory_match", {"i2c": True, "onewire": True}),))
    assert report.passed, report.outcomes


def test_inventory_match_catches_stale_report():
    # the classic silent failure: the agent reports yesterday's bus (or the
    # values from the task dossier) while the real bus differs
    board = FakeRawFsBoard()
    judge = OpsJudge(
        transport_factory=lambda: board,
        transcript='INVENTORY: {"i2c": [118], "onewire_family": 40}\nCLAIM: SUCCESS',
    )
    report = judge.run((OpsCheck("inventory_match", {"i2c": True, "onewire": True}),))
    assert not report.passed
    assert "i2c" in report.outcomes[0].detail


def test_inventory_match_requires_the_line():
    board = FakeRawFsBoard()
    judge = OpsJudge(transport_factory=lambda: board, transcript="the bus looks fine\nCLAIM: SUCCESS")
    report = judge.run((OpsCheck("inventory_match", {"i2c": True}),))
    assert not report.passed
    assert "INVENTORY" in report.outcomes[0].detail


def test_inventory_match_rejects_broken_json():
    board = FakeRawFsBoard()
    judge = OpsJudge(transport_factory=lambda: board, transcript='INVENTORY: {"i2c": [60, 118}')
    report = judge.run((OpsCheck("inventory_match", {"i2c": True}),))
    assert not report.passed


# ---------------------------------------------------------------------------
# mqtt_collect (real sim broker, real paho client on the default path)


def _publish_once(host: str, port: int, topic: str, payload: str):
    def run():
        time.sleep(0.3)
        pub = MqttTransport(host, port=port, client_id="ops-test-pub")
        pub.open()
        pub.publish(topic, payload)
        time.sleep(0.2)
        pub.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_mqtt_collect_matches_payload(monkeypatch):
    broker = MqttSimBroker()
    port = broker.start()
    try:
        monkeypatch.setenv("OPS_AB_MQTT_HOST", "127.0.0.1")
        monkeypatch.setenv("OPS_AB_MQTT_PORT", str(port))
        _publish_once("127.0.0.1", port, "bench/meteo/data", "T=25.7 C  P=1004.7 hPa")
        judge = OpsJudge(transport_factory=FakeBootBoard)
        report = judge.run(
            (OpsCheck("mqtt_collect", {"topic": "bench/meteo/data", "expect_regex": r"T=", "within_sec": 20}),)
        )
        assert report.passed, report.outcomes
    finally:
        broker.stop()


def test_mqtt_collect_fails_without_match(monkeypatch):
    broker = MqttSimBroker()
    port = broker.start()
    try:
        monkeypatch.setenv("OPS_AB_MQTT_HOST", "127.0.0.1")
        monkeypatch.setenv("OPS_AB_MQTT_PORT", str(port))
        _publish_once("127.0.0.1", port, "bench/meteo/data", "HUM=55")
        judge = OpsJudge(transport_factory=FakeBootBoard)
        report = judge.run(
            (OpsCheck("mqtt_collect", {"topic": "bench/meteo/data", "expect_regex": r"T=", "within_sec": 3}),)
        )
        assert not report.passed
    finally:
        broker.stop()


def test_mqtt_collect_requires_coordinates(monkeypatch):
    monkeypatch.delenv("OPS_AB_MQTT_HOST", raising=False)
    monkeypatch.delenv("OPS_AB_MQTT_PORT", raising=False)
    judge = OpsJudge(transport_factory=FakeBootBoard)
    report = judge.run((OpsCheck("mqtt_collect", {"topic": "t", "expect_regex": "x"}),))
    assert not report.passed
    assert "judge error" in report.outcomes[0].detail


# ---------------------------------------------------------------------------
# containment


def test_dead_board_is_contained_evidence_not_crash():
    def dead_factory():
        raise ConnectionError("port gone")

    judge = OpsJudge(transport_factory=dead_factory)
    report = judge.run(
        (
            OpsCheck("boot_expect", {"literals": ["X"], "within_sec": 3}),
            OpsCheck("device_file", {"path": "/m", "contains": "x"}),
        )
    )
    assert not report.passed
    assert all("judge error" in o.detail for o in report.outcomes)
    assert len(report.outcomes) == 2  # both checks ran, neither crashed the judge


def test_judge_needs_exactly_one_transport_source():
    with pytest.raises(ValueError):
        OpsJudge()
    with pytest.raises(ValueError):
        OpsJudge(port="COM1", transport_factory=FakeBootBoard)
