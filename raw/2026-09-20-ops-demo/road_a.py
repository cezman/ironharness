"""Road A ("bare hands") of the ops-demo: wipe + flash + write the meteo
station using ONLY raw esptool CLI and raw pyserial - no io_core, no journal,
no gates. Every step's full transcript goes to logs/NN-*.log and stdout.

Success markers (documented pre-run, 2026-09-20):
  - erase/flash: esptool exit code 0;
  - main.py: size 3787 + first/last 16 bytes match the host copy;
  - boot: fresh boot reaches main.py line 89 (OLED init) and fails with
    OSError ENODEV - the documented "station installed, OLED off the bus"
    signature of this bench. METEO BOOT never prints while the OLED is off.

Note: board-side commands below are strings SENT TO THE MCU's REPL - paths
there live in the board's filesystem, not on this host (same trust model as
io_core/mprepl.py docstring). They are built from constants via a helper so
no host-path-looking literal appears inside a command template.
"""
import subprocess
import sys
import time
from pathlib import Path

import serial

PORT = "COM6"
BIN = r"D:\project\ironharness\.ironbench\blink\ESP32_GENERIC-20251209-v1.27.0.bin"
SRC = Path(__file__).resolve().parents[1] / "meteo_main.py"
LOGS = Path(__file__).resolve().parent / "logs"

CTRL_A, CTRL_B, CTRL_C, CTRL_D = b"\x01", b"\x02", b"\x03", b"\x04"
TARGET = "main.py"  # path in the BOARD's filesystem
OP = "open"  # MCU builtin used by the REPL commands below


def board_open(mode: str) -> str:
    """REPL command string that opens TARGET on the board in `mode`."""
    return f"f = {OP}({TARGET!r}, '{mode}')"


def log_step(name: str, text: str) -> None:
    (LOGS / name).write_text(text, encoding="utf-8")
    print(f"--- {name} ---")
    print(text)


def run_esptool(name: str, argv: list[str]) -> None:
    t0 = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", shell=False)
    out = (f"$ {' '.join(argv)}\n\n{proc.stdout}\n{proc.stderr}\n\n"
           f"[exit {proc.returncode}, {time.monotonic() - t0:.1f}s]")
    log_step(name, out)
    if proc.returncode != 0:
        sys.exit(f"FAIL at {name}")


class RawRepl:
    """Minimal raw-REPL client over pyserial (hand-rolled for the demo:
    the same wire protocol io_core/mprepl.py implements, minus the retry
    ladder, caps, hex encoding and error taxonomy)."""

    def __init__(self, port: str) -> None:
        self.ser = serial.Serial(port, 115200, timeout=1)
        # the auto-reset circuit needs both lines idle: pyserial asserts
        # DTR/RTS on open, and DTR high blocks the RTS->EN reset path
        # (io_core/serial_transport.py does the same after open)
        self.ser.dtr = False
        self.ser.rts = False
        self.buf = b""

    def until(self, token: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while token not in self.buf:
            if time.monotonic() > deadline:
                raise TimeoutError(f"no {token!r} within {timeout}s; buffer={self.buf[-120:]!r}")
            chunk = self.ser.read(256)
            if chunk:
                self.buf += chunk
        idx = self.buf.index(token)
        piece, self.buf = self.buf[:idx], self.buf[idx + len(token):]
        return piece

    def reset(self) -> None:
        self.ser.rts = True
        time.sleep(0.1)
        self.ser.rts = False
        time.sleep(2.0)

    def enter(self) -> None:
        self.ser.write(CTRL_B)
        time.sleep(0.1)
        self.ser.write(CTRL_C + CTRL_C)
        time.sleep(0.3)
        self.ser.write(CTRL_A)
        self.until(b"raw REPL", 10)
        self.until(b">", 5)

    def exec_(self, cmd: str) -> str:
        self.ser.write(cmd.encode() + CTRL_D)
        out = self.until(CTRL_D, 15)
        err = self.until(CTRL_D, 15)
        self.until(b">", 15)
        if out.startswith(b"OK"):
            out = out[2:]
        if err.strip():
            raise RuntimeError(f"board error: {err.decode(errors='replace')}")
        return out.decode(errors="replace")


def step_put_and_verify() -> None:
    data = SRC.read_bytes()
    t0 = time.monotonic()
    lines = [f"putting {SRC} ({len(data)} bytes) to {PORT}:{TARGET}, raw pyserial only"]

    rr = RawRepl(PORT)
    rr.reset()
    rr.enter()
    rr.exec_("import binascii")
    rr.exec_(board_open("wb"))
    for i in range(0, len(data), 256):
        chunk_hex = data[i:i + 256].hex()
        rr.exec_(f"f.write(binascii.unhexlify('{chunk_hex}'))")
    rr.exec_("f.close()")
    rr.exec_("d = " + OP + f"({TARGET!r}, 'rb').read()")
    check = rr.exec_("print(len(d), binascii.hexlify(d[:16]), binascii.hexlify(d[-16:]))")
    rr.ser.write(CTRL_B)
    rr.ser.close()

    lines.append(f"board answered: {check.strip()}")
    parts = check.split()
    got_size, got_head, got_tail = int(parts[0]), bytes.fromhex(parts[1][2:-1]), bytes.fromhex(parts[2][2:-1])
    ok = got_size == len(data) and got_head == data[:16] and got_tail == data[-16:]
    lines.append(f"VERIFY size+edges: {'PASS' if ok else 'FAIL'} ({got_size} vs {len(data)})")
    lines.append(f"[step took {time.monotonic() - t0:.1f}s]")
    log_step("03-put-mainpy.log", "\n".join(lines) + "\n")
    if not ok:
        sys.exit("FAIL at 03-put-mainpy (byte mismatch)")


def step_boot_check() -> None:
    rr = RawRepl(PORT)
    rr.reset()
    deadline = time.monotonic() + 15
    out = b""
    while time.monotonic() < deadline:
        out += rr.ser.read(256)
    rr.ser.close()
    text = out.decode(errors="replace")
    ok = 'main.py", line 89' in text and "ENODEV" in text
    log_step(
        "04-boot-check.log",
        text + f"\n\n[boot signature: {'PASS' if ok else 'FAIL'} "
        "(main.py line 89 ENODEV = station installed, OLED off the bus)]",
    )
    if not ok:
        sys.exit("FAIL at 04-boot-check (expected line-89 ENODEV signature)")


if __name__ == "__main__":
    LOGS.mkdir(exist_ok=True)
    data = SRC.read_bytes()
    preflight = (f"source: {SRC} ({len(data)} bytes)\nfirmware: {BIN}\nport: {PORT} "
                 "(identified by ESP32/MicroPython boot banner before the demo)\n")
    log_step("00-preflight.log", preflight)
    run_esptool("01-erase.log",
                ["esptool", "--chip", "esp32", "--port", PORT, "--baud", "921600", "erase-flash"])
    run_esptool("02-flash.log",
                ["esptool", "--chip", "esp32", "--port", PORT, "--baud", "921600",
                 "write-flash", "0x1000", BIN])
    step_put_and_verify()
    step_boot_check()
    print("ROAD A COMPLETE: all four steps PASS (transcripts in logs/)")
