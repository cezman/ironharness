"""Road B (MCP) of the ops-demo: the SAME wipe + flash + station write, done
exclusively through io_core MCP tools spoken over the stdio JSON-RPC wire by
a dedicated server subprocess.

Two phases, one transcript, one journal (mcp-home/journal.jsonl):

  NEG (no IRONHARNESS_ALLOW_REAL_FLASH): esp_erase must be REFUSED and the
      refusal journaled (esp_denied) - the gate is the demo's safety beat.
  POS (IRONHARNESS_ALLOW_REAL_FLASH=1): serial_open -> reset -> read boot
      beat -> esp_erase -> esp_flash -> file_write -> serial_put -> reset ->
      read boot signature -> serial_get round-trip byte-compare.

Everything the agent does is a tools/call; the server journals who/when/
what/response - the journal is the demo's evidence artifact.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
HOME = HERE.parent / "mcp-home"
SRC = HERE.parent / "meteo_main.py"
BIN = r"D:\project\ironharness\.ironbench\blink\ESP32_GENERIC-20251209-v1.27.0.bin"
PORT = "COM6"

TRANSCRIPT: list[str] = []


def log(line: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    TRANSCRIPT.append(f"[{stamp}] {line}")
    print(f"[{stamp}] {line}", flush=True)


def spawn_server(allow_flash: bool) -> subprocess.Popen:
    env = {
        **os.environ,
        "IRONHARNESS_HOME": str(HOME),
        "IRONHARNESS_SANDBOX": str(HOME / "sandbox"),
    }
    if allow_flash:
        env["IRONHARNESS_ALLOW_REAL_FLASH"] = "1"
    else:
        env.pop("IRONHARNESS_ALLOW_REAL_FLASH", None)
    return subprocess.Popen(
        [sys.executable, "-m", "io_core.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1, env=env,
    )


class WireClient:
    """Line-delimited JSON-RPC over the server's stdio (the pattern from
    tests/test_mcp_stdio.py - what any third-party MCP client speaks)."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.messages: queue.Queue = queue.Queue()
        self._next_id = 10
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    self.messages.put({"__stdout_garbage__": line})
        self.messages.put(None)

    def request(self, payload: dict, timeout: float) -> dict:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no response in {timeout}s: {payload.get('method')}")
            try:
                msg = self.messages.get(timeout=remaining)
            except queue.Empty as e:
                raise TimeoutError(f"no response in {timeout}s: {payload.get('method')}") from e
            if msg is None:
                raise AssertionError("server closed stdout before responding")
            if "__stdout_garbage__" in msg:
                raise AssertionError(f"non-JSON on server stdout: {msg['__stdout_garbage__']!r}")
            if msg.get("id") == payload.get("id"):
                return msg

    def call(self, name: str, arguments: dict, timeout: float = 120.0) -> str:
        t0 = time.monotonic()
        self._next_id += 1
        msg = self.request({
            "jsonrpc": "2.0", "id": self._next_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, timeout)
        if "error" in msg:
            raise RuntimeError(f"{name}: wire error {msg['error']}")
        result = msg["result"]
        text = "\n".join(c.get("text", "") for c in result.get("content", []))
        marker = "ERROR" if result.get("isError") else "ok"
        log(f"  -> {name}({', '.join(f'{k}={v!r}' for k, v in arguments.items())}) "
            f"[{marker}, {time.monotonic() - t0:.1f}s] {text[:300]}")
        if result.get("isError"):
            raise RuntimeError(f"{name} refused: {text}")
        return text

    def initialize(self) -> dict:
        self._next_id += 1
        init = self.request({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "ops-demo", "version": "0"},
            },
        }, 30)
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()
        return init["result"]


def read_boot_beat(client: WireClient, expect: str) -> str:
    """serial_reset then wait for `expect` in the boot output."""
    client.call("serial_reset", {"name": "board", "settle_sec": 2})
    client.call("serial_reader_start", {"name": "board"})
    out = client.call("serial_read_until", {"name": "board", "pattern": expect, "timeout": 12})
    client.call("serial_reader_stop", {"name": "board"})
    return out


def dump_journal_events() -> None:
    journal = HOME / "journal.jsonl"
    log("--- journal evidence (mcp-home/journal.jsonl) ---")
    for line in journal.read_text(encoding="utf-8").splitlines():
        ev = json.loads(line)
        kind, who, ts = ev.get("kind"), ev.get("actor"), ev.get("ts")
        payload = {k: v for k, v in ev.items() if k not in ("kind", "actor", "ts", "seq")}
        log(f"  journal: actor={who} kind={kind} {json.dumps(payload, ensure_ascii=False)[:220]}")
    log(f"--- journal lines total: {sum(1 for _ in journal.open())} ---")


def main() -> None:
    LOGS.mkdir(exist_ok=True)
    HOME.mkdir(exist_ok=True)

    # --- phase NEG: the gate must refuse and journal the refusal ---
    log("PHASE NEG: spawn server WITHOUT IRONHARNESS_ALLOW_REAL_FLASH")
    proc = spawn_server(allow_flash=False)
    client = WireClient(proc)
    info = client.initialize()
    log(f"initialized: server={info['serverInfo']['name']} protocol={info['protocolVersion']}")
    listing = client.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, 30)
    tools = {t["name"]: t for t in listing["result"]["tools"]}
    log(f"tools/list: {len(tools)} tools; esp_erase annotations="
        f"{tools['esp_erase']['annotations']}")
    try:
        client.call("esp_erase", {"port": PORT}, timeout=60)
        sys.exit("FAIL: esp_erase was ALLOWED without the opt-in flag")
    except RuntimeError as e:
        log(f"  -> gate REFUSED as designed: {e}")
    proc.kill()
    proc.wait(timeout=10)

    # --- phase POS: opt-in, then the full wipe + flash + station write ---
    log("PHASE POS: spawn server WITH IRONHARNESS_ALLOW_REAL_FLASH=1")
    proc = spawn_server(allow_flash=True)
    client = WireClient(proc)
    client.initialize()
    client.call("serial_open", {"name": "board", "port": PORT, "timeout": 2})
    read_boot_beat(client, expect="Traceback")  # alive beat: current meteo boot signature
    log("closing the transport: esptool needs the port exclusively")
    client.call("serial_close", {"name": "board"})
    client.call("esp_erase", {"port": PORT}, timeout=180)
    client.call("esp_flash", {"port": PORT, "firmware_path": BIN, "addr": 4096}, timeout=300)
    client.call("file_write", {"path": "main.py", "content": SRC.read_text(encoding="utf-8")})
    client.call("serial_open", {"name": "board", "port": PORT, "timeout": 2})
    client.call("serial_put", {"name": "board", "source_path": "main.py", "target_path": "main.py"},
                timeout=300)
    out = read_boot_beat(client, expect="Traceback")
    log(f"  -> fresh boot signature: {'main.py line 89 ENODEV' if 'line 89' in out else 'UNEXPECTED'}")

    # round-trip proof: pull main.py back and byte-compare host-side
    client.call("serial_get", {"name": "board", "target_path": "main.py", "dest_path": "verify-main.py"},
                timeout=300)
    pulled = (HOME / "sandbox" / "verify-main.py").read_bytes()
    ok = pulled == SRC.read_bytes()
    log(f"ROUND-TRIP serial_get byte-compare: {'PASS' if ok else 'FAIL'} ({len(pulled)} bytes)")
    client.call("session_status", {})
    client.call("serial_reset", {"name": "board", "settle_sec": 2})
    client.call("serial_close", {"name": "board"})
    proc.kill()
    proc.wait(timeout=10)

    dump_journal_events()
    if not ok:
        sys.exit("FAIL: round-trip mismatch")
    log("ROAD B COMPLETE: gate refusal + wipe + flash + station write + round-trip, "
        "every step journaled")
    (LOGS / "transcript.log").write_text("\n".join(TRANSCRIPT) + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        (LOGS / "transcript.log").write_text("\n".join(TRANSCRIPT) + "\n", encoding="utf-8")
        raise
