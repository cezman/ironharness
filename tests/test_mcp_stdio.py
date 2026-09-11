"""MCP stdio smoke tests (IH-30): the server's stdio transport was untested
since the first commit ("checked manually"). Two independent clients drive the
real subprocess over the real wire:

1. a hand-rolled line-delimited JSON-RPC client - knows nothing about the
   server's Python API, only the protocol (this is what any third-party
   client speaks). Flushed non-JSON garbage on the server's stdout fails it
   by design - this is the IH-20 hygiene guard this stand enables;
2. the official MCP SDK client (ClientSession over stdio_client) - the
   compatibility pin ("independent client" backlog idea, mcp-use #2). The
   SDK client tolerates garbage lines, so the garbage guard lives in (1).

Both spawn `python -m io_core.mcp_server` with IRONHARNESS_HOME/SANDBOX in a
tmp dir, and the journal-exists assertion pins that no session lands in the
developer's real ~/.ironharness.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = str(REPO / "src")
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "ironharness-smoke", "version": "0"},
    },
}


def spawn_server(tmp_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "PYTHONPATH": SRC,
        "IRONHARNESS_HOME": str(tmp_path / "home"),
        "IRONHARNESS_SANDBOX": str(tmp_path / "home" / "sandbox"),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "io_core.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
    )


class WireClient:
    """Line-delimited JSON-RPC over the subprocess pipes; a reader thread
    keeps responses flowing while we block on writes."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.messages: queue.Queue = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    self.messages.put({"__stdout_garbage__": line})
        self.messages.put(None)

    def request(self, payload: dict, timeout: float = 20.0) -> dict:
        assert self.proc.poll() is None, "server exited before the request"
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        deadline = timeout
        while True:
            try:
                msg = self.messages.get(timeout=deadline)
            except queue.Empty as e:
                raise AssertionError(f"no JSON-RPC response in {timeout}s") from e
            if msg is None:
                raise AssertionError("server closed stdout before responding")
            if "__stdout_garbage__" in msg:
                raise AssertionError(f"non-JSON on the server stdout: {msg}")
            if msg.get("id") == payload.get("id"):
                return msg
            # server-initiated messages (notifications/requests) pass by

    def notify(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()


@pytest.fixture()
def server(tmp_path):
    proc = spawn_server(tmp_path)
    yield proc
    proc.kill()
    proc.wait(timeout=10)


def test_wire_initialize_tools_call(server, tmp_path):
    client = WireClient(server)
    init = client.request(INITIALIZE)
    assert "error" not in init
    assert init["result"]["serverInfo"]["name"] == "ironharness"
    assert "protocolVersion" in init["result"]
    client.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    listing = client.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {t["name"] for t in listing["result"]["tools"]}
    assert {"echo", "serial_open", "file_list", "esp_flash"} <= names

    call = client.request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "ping"}},
        }
    )
    assert "error" not in call
    assert call["result"]["content"][0]["text"] == "ping"

    # a tool call before any session exists must not crash the server: the
    # file_list call creates the session against IRONHARNESS_HOME from tmp
    files = client.request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "file_list", "arguments": {"path": "."}},
        }
    )
    assert "error" not in files
    assert files["result"].get("isError") is not True, files
    # the tmp-home isolation is pinned, not just declared: the session the
    # server just created must have journaled into tmp, never ~/.ironharness
    assert (tmp_path / "home" / "journal.jsonl").is_file()


def test_wire_unknown_tool_is_a_clean_jsonrpc_error(server):
    client = WireClient(server)
    client.request(INITIALIZE)
    client.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
    call = client.request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        }
    )
    # protocol-level behavior: either a JSON-RPC error object or an MCP
    # isError result - never a crashed server
    assert server.poll() is None, "server died on an unknown tool"
    assert ("error" in call) or (call["result"].get("isError") is True), call


def test_sdk_client_end_to_end(tmp_path):
    anyio = pytest.importorskip("anyio")
    pytest.importorskip("mcp.client.stdio")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import get_default_environment, stdio_client

    env = get_default_environment()
    env["PYTHONPATH"] = SRC
    env["IRONHARNESS_HOME"] = str(tmp_path / "home")
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "io_core.mcp_server"], env=env
    )

    async def drive():
        async with stdio_client(params) as (read, write):  # noqa: SIM117 - SDK idiom
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                assert info.server_info.name == "ironharness"
                tools = await session.list_tools()
                assert "echo" in {t.name for t in tools.tools}
                res = await session.call_tool("echo", {"text": "sdk-ping"})
                assert res.content[0].text == "sdk-ping"

    anyio.run(drive)
