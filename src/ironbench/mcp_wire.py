"""Stdio JSON-RPC client for driving the io-core MCP server (IH-105).

The MCP arm of the ops A/B talks to a REAL server process, not to a
library re-implementation: the measured tool surface must be the one
external agents get, wire format included (the checklist's rule about
transport layers counts). This module is the client half of that wire:
line-delimited JSON-RPC over the server's stdio, one reader thread, a
garbage-on-stdout tripwire, and tool calls that come back as data
(ok/text) because a failed tool call is an observation for the agent,
not an exception of the runner.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


class McpWireError(RuntimeError):
    """The server process broke the wire (exit, non-JSON stdout, timeout)."""


class McpWireClient:
    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._messages: queue.Queue = queue.Queue()
        self._next_id = 1
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._messages.put(json.loads(line))
            except json.JSONDecodeError:
                self._messages.put({"__stdout_garbage__": line})
        self._messages.put(None)

    def request(self, payload: dict, timeout: float = 30.0) -> dict:
        if self.proc.poll() is not None:
            raise McpWireError("server exited before the request")
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no JSON-RPC response within {timeout}s")
            try:
                msg = self._messages.get(timeout=remaining)
            except queue.Empty as e:
                raise TimeoutError(f"no JSON-RPC response within {timeout}s") from e
            if msg is None:
                raise McpWireError("server closed stdout before responding")
            if "__stdout_garbage__" in msg:
                raise McpWireError(f"non-JSON on the server stdout: {msg!r}")
            if msg.get("id") == payload.get("id"):
                return msg
            # server-initiated notifications/requests pass by

    def notify(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def initialize(self, timeout: float = 30.0) -> None:
        init = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ironbench-ops", "version": "0"},
            },
        }
        self._next_id += 1
        resp = self.request(init, timeout=timeout)
        if "error" in resp:
            raise McpWireError(f"initialize failed: {resp['error']}")
        self.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self, timeout: float = 30.0) -> list[dict]:
        self._next_id += 1
        resp = self.request(
            {"jsonrpc": "2.0", "id": self._next_id, "method": "tools/list", "params": {}},
            timeout=timeout,
        )
        if "error" in resp:
            raise McpWireError(f"tools/list failed: {resp['error']}")
        return resp["result"]["tools"]

    def call_tool(self, name: str, arguments: dict, timeout: float = 120.0) -> dict:
        """Returns {"ok": bool, "text": ...}; a failed tool call is an
        observation for the agent, not a runner error."""
        self._next_id += 1
        resp = self.request(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            timeout=timeout,
        )
        if "error" in resp:
            return {"ok": False, "text": f"jsonrpc error: {resp['error']}"}
        result = resp.get("result") or {}
        parts = result.get("content") or []
        text = "\n".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
        if result.get("isError"):
            return {"ok": False, "text": text or "tool error"}
        return {"ok": True, "text": text}

    def close(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def spawn_mcp_client(env_overrides: dict[str, str], *, cwd: Path | None = None) -> McpWireClient:
    """Spawns `python -m io_core.mcp_server` and completes the handshake."""
    env = {
        **os.environ,
        **env_overrides,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "io_core.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
        cwd=str(cwd) if cwd else None,
    )
    client = McpWireClient(proc)
    client.initialize()
    return client
