"""The ops attempt runner end to end, offline (IH-105): preflight gate,
seeded setup, an MCP arm driven by a scripted LLM against a fake board,
the state judge, and the metrics row - including the headline case where
an agent CLAIMS SUCCESS the judge refutes (silent failure)."""

import ast
import json
import re
import urllib.error
from pathlib import Path

import pytest

from ironbench.agent import ChatReply, SolveConfig
from ironbench.cli import main as cli_main
from ironbench.ops_run import (
    OpsSetupError,
    count_journal_ops,
    count_script_ops,
    journal_coverage,
    ops_preflight,
    run_ops_attempt,
    run_steps,
)
from ironbench.ops_tasks import OpsAsset, OpsBudget, OpsCheck, OpsStep, OpsTask

CFG = SolveConfig(base_url="http://localhost:1234/v1", api_key="x", model="test-model")

GOLDEN = b"print('METEO BOOT')\nprint('T=25.00 C')\n"
BROKEN = b"import meteo_app\nmeteo_app.run()\n"


class FakeBoard:
    """Raw-REPL filesystem + scripted boot output: enough protocol to serve
    put/get (device_file checks, deploy steps) and boot_expect."""

    def __init__(self, files=None, boot_lines=("METEO BOOT", "T=25.00 C")):
        self.files = {k: bytearray(v) for k, v in (files or {}).items()}
        self.boot_lines = list(boot_lines)
        self._inbuf = bytearray()
        self._outbuf = bytearray()
        self._raw = False
        self._f = None

    # boot side
    def reset(self, *, pulse_sec=0.1, settle_sec=2.0):
        self._outbuf += ("\r\n".join(self.boot_lines) + "\r\n").encode()

    @property
    def in_waiting(self):
        return len(self._outbuf)

    # serial side
    def read(self, size=1):
        chunk = bytes(self._outbuf[:size])
        del self._outbuf[:size]
        return chunk

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
                path, mode, wpos, _rpos = self._f
                piece = bytes.fromhex(m.group(1))
                buf = self.files.setdefault(path, bytearray())
                end = wpos + len(piece)
                buf.extend(b"\x00" * (end - len(buf)))
                buf[wpos:end] = piece
                self._f = (path, mode, end, _rpos)
            elif m := re.match(r"^print\(binascii\.hexlify\(f\.read\((\d+)\)\)\)$", command):
                path, _mode, wpos, rpos = self._f
                size = int(m.group(1))
                chunk = bytes(self.files.get(path, bytearray())[rpos : rpos + size])
                self._f = (path, _mode, wpos, rpos + len(chunk))
                out.append(("b'" + chunk.hex() + "'").encode() + b"\r\n")
            elif command == "f.close()":
                self._f = None
            elif command.startswith("import os; os.remove("):
                path = ast.literal_eval(command[len("import os; os.remove(") : -1])
                if path not in self.files:
                    raise OSError(2, "ENOENT")
                del self.files[path]
            else:
                raise SyntaxError(f"invalid command: {command!r}")
        except Exception as e:  # noqa: BLE001 - the board answers with a traceback
            self._fail(f"{type(e).__name__}: {e}")
            return
        self._emit(b"OK" + b"".join(out) + b"\x04\x04>")

    def close(self):
        pass


class FakeMcpClient:
    """Applies tool calls to the FakeBoard + a sandbox dir, like the real
    io-core server would."""

    def __init__(self, board: FakeBoard, sandbox: Path):
        self.board = board
        self.sandbox = sandbox
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self):
        return [
            {"name": "serial_open", "description": "open", "inputSchema": {"properties": {}}},
            {"name": "serial_put", "description": "put", "inputSchema": {"properties": {}}},
            {"name": "file_write", "description": "write", "inputSchema": {"properties": {}}},
        ]

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "file_write":
            path = self.sandbox / arguments["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(arguments["content"], encoding="utf-8", newline="")
            return {"ok": True, "text": f"wrote {arguments['path']}"}
        if name == "serial_open":
            return {"ok": True, "text": "ok: serial"}
        if name == "serial_put":
            data = (self.sandbox / arguments["source"]).read_bytes()
            self.board.files[arguments["target"]] = bytearray(data)
            return {"ok": True, "text": f"put {len(data)}B"}
        return {"ok": False, "text": f"unknown tool {name}"}

    def close(self):
        pass


def make_task(tmp_path: Path) -> OpsTask:
    (tmp_path / "golden.py").write_bytes(GOLDEN)
    (tmp_path / "broken.py").write_bytes(BROKEN)
    return OpsTask(
        name="ops-mini",
        description="restore the station from the golden copy",
        wall_sec=60,
        budget=OpsBudget(max_iterations=5, iter_timeout_sec=20),
        judge=(
            OpsCheck("boot_expect", {"literals": ["METEO BOOT"], "within_sec": 5}),
            OpsCheck("device_file", {"path": "/main.py", "asset": "golden"}),
        ),
        tags=("io",),
        allowed_tools=("serial", "file"),
        assets=(
            OpsAsset(name="golden", path="golden.py"),
            OpsAsset(name="broken_ref", path="broken.py"),
        ),
        setup=(OpsStep("deploy_file", {"asset": "broken_ref", "target": "/main.py"}),),
        restore=(),
        source_dir=tmp_path,
    )


def test_preflight_passes_on_boot_evidence():
    board = FakeBoard()
    assert "METEO BOOT" in ops_preflight("COM9", transport_factory=lambda: board, seconds=2)


def test_preflight_refuses_silent_port():
    board = FakeBoard(boot_lines=[])
    with pytest.raises(ConnectionError, match="boot evidence"):
        ops_preflight("COM9", transport_factory=lambda: board, seconds=1)


def test_run_steps_deploy_and_round_trip(tmp_path):
    (tmp_path / "golden.py").write_bytes(GOLDEN)
    board = FakeBoard()
    from io_core.journal import JsonlJournal

    journal = JsonlJournal(tmp_path / "h.jsonl", actor="t")
    run_steps(
        (OpsStep("deploy_file", {"asset": "golden", "target": "/main.py"}),),
        port="COM9",
        assets={"golden": tmp_path / "golden.py"},
        journal=journal,
        phase="setup",
        transport_factory=lambda: board,
    )
    assert bytes(board.files["/main.py"]) == GOLDEN


def test_run_steps_failure_is_typed(tmp_path):
    board = FakeBoard()
    from io_core.journal import JsonlJournal

    journal = JsonlJournal(tmp_path / "h.jsonl", actor="t")
    with pytest.raises(OpsSetupError):
        run_steps(
            (OpsStep("deploy_file", {"asset": "absent", "target": "/main.py"}),),
            port="COM9",
            assets={},
            journal=journal,
            phase="setup",
            transport_factory=lambda: board,
        )


def _run(task, tmp_path, *, replies, board, sandbox_name="sandbox", preflight=True):
    sandbox = tmp_path / sandbox_name
    sandbox.mkdir(exist_ok=True)
    steps = iter(replies)

    def llm(cfg, messages):
        return next(steps)

    def client_factory():
        return FakeMcpClient(board, sandbox)

    return run_ops_attempt(
        task,
        arm="mcp",
        model="test-model",
        attempt=1,
        port="COM9",
        out_dir=tmp_path / "out",
        llm_cfg=CFG,
        llm=llm,
        transport_factory=lambda: board,
        mcp_client_factory=client_factory,
        preflight=preflight,
    )


def test_attempt_end_to_end_solved(tmp_path):
    task = make_task(tmp_path)
    board = FakeBoard()
    sandbox = tmp_path / "sb"
    sandbox.mkdir()

    calls = iter(
        [
            ChatReply(content='{"tool": "file_write", "arguments": {"path": "g.py", "content": '
                             + json.dumps(GOLDEN.decode()) + "}}"),
            ChatReply(content='{"tool": "serial_put", "arguments": {"name": "b", "source": "g.py", "target": "/main.py"}}'),
            ChatReply(content='{"claim": "SUCCESS"}'),
        ]
    )

    def llm(cfg, messages):
        return next(calls)

    def client_factory():
        return FakeMcpClient(board, sandbox)

    result = run_ops_attempt(
        task,
        arm="mcp",
        model="test-model",
        attempt=1,
        port="COM9",
        out_dir=tmp_path / "out",
        llm_cfg=CFG,
        llm=llm,
        transport_factory=lambda: board,
        mcp_client_factory=client_factory,
        preflight=False,
    )
    assert result.solved is True
    assert result.silent_failure is False
    assert result.claimed == "SUCCESS"
    assert result.judge["passed"] is True
    assert result.iterations == 3
    assert (result.run_dir / "result.json").is_file()
    assert (result.run_dir / "transcript.txt").is_file()
    assert (result.run_dir / "judge.json").is_file()


def test_attempt_silent_failure_claim_refuted_by_judge(tmp_path):
    task = make_task(tmp_path)
    board = FakeBoard()

    def llm(cfg, messages):
        return ChatReply(content='{"claim": "SUCCESS"}')

    result = run_ops_attempt(
        task,
        arm="mcp",
        model="test-model",
        attempt=1,
        port="COM9",
        out_dir=tmp_path / "out",
        llm_cfg=CFG,
        llm=llm,
        transport_factory=lambda: board,
        mcp_client_factory=lambda: FakeMcpClient(board, tmp_path / "sb2"),
        preflight=False,
    )
    # the agent claimed SUCCESS without touching the board; the judge
    # refutes the claim - the headline silent-failure metric
    assert result.solved is False
    assert result.silent_failure is True
    assert result.judge["passed"] is False


def test_attempt_infra_when_llm_is_down(tmp_path):
    task = make_task(tmp_path)
    board = FakeBoard()

    def llm(cfg, messages):
        raise urllib.error.URLError("connection refused")

    result = run_ops_attempt(
        task,
        arm="mcp",
        model="test-model",
        attempt=1,
        port="COM9",
        out_dir=tmp_path / "out",
        llm_cfg=CFG,
        llm=llm,
        transport_factory=lambda: board,
        mcp_client_factory=lambda: FakeMcpClient(board, tmp_path / "sb3"),
        preflight=False,
    )
    assert result.error_kind == "infra"
    assert result.judge == {}


def test_journal_coverage_counts_both_sides(tmp_path):
    scripts = [
        "import serial\ns = serial.Serial('COM9', timeout=1)\ns.write(b'x')",
        "import esptool",
    ]
    assert count_script_ops(scripts) == 2  # one serial open + one esptool run
    # nothing journaled -> 0.0; nothing declared -> None (nothing to cover)
    assert journal_coverage(3, [tmp_path / "absent.jsonl"]) == 0.0
    assert journal_coverage(0, [tmp_path / "absent.jsonl"]) is None
    (tmp_path / "j.jsonl").write_text(
        "\n".join(
            json.dumps({"kind": kind})
            for kind in ("serial_open", "serial_write", "esp_flash", "noise")
        )
        + "\n",
        encoding="utf-8",
    )
    assert count_journal_ops([tmp_path / "j.jsonl"]) == 3
    assert journal_coverage(3, [tmp_path / "j.jsonl"]) == 1.0


def test_cli_ops_ab_refuses_without_allow_real(tmp_path, capsys):
    rc = cli_main(
        ["ops-ab", "--task", "ops-mini", "--port", "COM9", "--out", str(tmp_path / "out")]
    )
    assert rc == 2
    assert "allow-real" in capsys.readouterr().out
