"""The two A/B arms (IH-105): same model, same dossier, different tools.

- BARE: the model writes host Python (pyserial / esptool are installed);
  the runner executes each script as a subprocess and feeds its output
  back as the next observation.
- MCP: the model drives a real io-core MCP server process over stdio,
  one JSON tool call per turn.

Fairness contract: both arms receive the same task description, the same
dossier facts and the same iteration/wall budget. They differ ONLY in the
tool surface - that difference is the measured quantity. Every attempt
ends with an explicit agent verdict (the claim protocol); the state judge
grades the claim, and a SUCCESS claim the judge refutes is the headline
metric (silent failure).

Trust boundary (documented, not hidden): the BARE arm executes
model-authored Python on the host with the runner's privileges, bounded
by a subprocess timeout and output caps. That is scheduling, not
isolation - the same trust the MCP arm places in tool arguments reaching
real hardware. The bench host is disposable by design.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ironbench.agent import ChatReply, SolveConfig, chat_completion, extract_code
from ironbench.mcp_wire import McpWireClient

OBSERVATION_CAP = 4 * 1024
LLM_RETRIES = 3
LLM_RETRY_BACKOFF_SEC = 10.0


def chat_with_retry(cfg: SolveConfig, messages: list[dict]) -> ChatReply:
    """chat_completion with short retries: the local server hiccups
    (a JIT-loading model answers 400/5xx, the daemon restarts) must cost a
    retry, not the whole attempt. Deterministic 400s (prompt overflow) just
    pay the backoff before the attempt is classified infra."""
    last: Exception | None = None
    for i in range(LLM_RETRIES):
        try:
            return chat_completion(cfg, messages)
        except OSError as e:  # URLError/HTTPError/timeout are OSError
            last = e
            if i + 1 < LLM_RETRIES:
                time.sleep(LLM_RETRY_BACKOFF_SEC)
    raise last  # type: ignore[misc]
CLAIM_RE = re.compile(r"^CLAIM:\s*(SUCCESS|FAIL)\s*$", re.MULTILINE)
# state-changing tool families the coverage metric counts as operations
JOURNALED_TOOL_RE = re.compile(r"^(serial_|esp_|file_|mqtt_)")
# host ports named inside a bare script - anything but the assigned port is
# the wrong-port incident class (two CH340s live on this bench)
PORT_RE = re.compile(r"\b(COM\d+|/dev/(?:ttyUSB|ttyACM|tty\.)\d+|loop://\S+|socket://\S+)")


@dataclasses.dataclass
class Turn:
    iteration: int
    kind: str  # script | tool | claim | invalid
    content: str  # the model's reply (or extracted payload)
    observation: str = ""
    ok: bool = True


@dataclasses.dataclass
class ArmResult:
    turns: list[Turn]
    claimed: str | None  # "SUCCESS" / "FAIL" / None (budget exhausted)
    claim_iteration: int | None
    tokens_in: int = 0
    tokens_out: int = 0
    timed_out: bool = False

    @property
    def iterations(self) -> int:
        return len(self.turns)

    def transcript(self) -> str:
        lines = []
        for turn in self.turns:
            lines.append(f"=== iter {turn.iteration} [{turn.kind}] ===")
            lines.append(turn.content)
            if turn.observation:
                lines.append(f"--- observation ---\n{turn.observation}")
        return "\n".join(lines)


def _cap(text: str, cap: int = OBSERVATION_CAP) -> str:
    if len(text) <= cap:
        return text
    return f"[... {len(text) - cap} bytes truncated ...]\n" + text[-cap:]


def parse_claim(text: str) -> str | None:
    claims = CLAIM_RE.findall(text)
    return claims[-1] if claims else None


def extract_last_json(text: str) -> dict | None:
    """The last top-level JSON object in a reply. Scans left to right with
    raw_decode, jumping past each parsed object: nested objects and braces
    inside strings stay inside their parent, prose braces are skipped."""
    decoder = json.JSONDecoder()
    last = None
    pos = 0
    while True:
        brace = text.find("{", pos)
        if brace < 0:
            return last
        try:
            value, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            pos = brace + 1
            continue
        if isinstance(value, dict):
            last = value
        pos = end


BARE_SYSTEM_PROMPT = (
    "You operate a live embedded board from a host computer. You work in "
    "iterations: reply with exactly one ```python block containing a complete "
    "host Python script (Python 3.12; pyserial is installed; the board's "
    "serial port is in the environment variable IRONBENCH_OPS_PORT). The "
    "script runs with a timeout, and you will see its combined stdout/stderr "
    "as feedback. When your evidence shows the task is done, reply with the "
    "single line 'CLAIM: SUCCESS' (no code block). If you conclude the task "
    "cannot be done, reply 'CLAIM: FAIL'. Never claim success without "
    "evidence from the board."
)

MCP_SYSTEM_PROMPT = (
    "You operate a live embedded board through hardware tools. You work in "
    "iterations: call exactly one tool per turn; you will see its result as "
    "feedback. When your evidence shows the task is done, call the claim tool "
    "with verdict SUCCESS; if the task cannot be done, call it with verdict "
    "FAIL. Never claim success without evidence from the board."
)

CLAIM_TOOL = {
    "type": "function",
    "function": {
        "name": "claim",
        "description": "End the attempt with your verdict about the task.",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["SUCCESS", "FAIL"]},
                "inventory": {
                    "type": "object",
                    "description": "Optional: reported inventory data (task-specific).",
                },
            },
            "required": ["verdict"],
        },
    },
}


def native_tools(catalog: list[dict]) -> list[dict]:
    """MCP tool descriptors in OpenAI function-tool form plus the claim tool."""
    out = []
    for tool in catalog:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out + [CLAIM_TOOL]


def parse_tool_arguments(raw: Any) -> dict:
    """OpenAI tool arguments arrive as a JSON string (or a dict)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def tool_catalog(catalog: list[dict], allowed_families: tuple[str, ...]) -> str:
    """Human-readable tool list (text-protocol fallback rendering)."""
    prefixes = tuple(f + "_" for f in allowed_families)
    lines = []
    for tool in catalog:
        name = tool.get("name", "")
        if not name.startswith(prefixes):
            continue
        desc = " ".join(str(tool.get("description", "")).split())
        schema = tool.get("inputSchema") or {}
        lines.append(f"- {name}: {desc}\n  args: {json.dumps(schema.get('properties', {}))}")
    return "\n".join(lines)


def scan_bare_accidents(scripts: list[str], assigned_port: str) -> list[str]:
    """Rule-based incident scan over the model's scripts (wrong port,
    reads that can block forever). Conservative: counts candidate matches,
    does not try to prove effect."""
    accidents: list[str] = []
    for script in scripts:
        ports = set(PORT_RE.findall(script)) - {assigned_port}
        if ports:
            accidents.append(f"wrong_port_reference:{min(ports)}")
        if re.search(r"serial\.Serial\((?![^)]*timeout\s*=)", script, re.DOTALL):
            accidents.append("serial_open_without_timeout")
    return accidents


class BareArm:
    """The no-tools arm: model-authored host scripts, executed one per turn."""

    def __init__(
        self,
        cfg: SolveConfig,
        *,
        port: str,
        workdir: Path,
        iter_timeout_sec: int,
    ) -> None:
        self._cfg = cfg
        self._port = port
        self._workdir = Path(workdir)
        self._iter_timeout = iter_timeout_sec

    def run(
        self,
        *,
        task_prompt: str,
        max_iterations: int,
        deadline: float,
        llm: Any = None,
    ) -> ArmResult:
        chat = llm or chat_with_retry
        result = ArmResult(turns=[], claimed=None, claim_iteration=None)
        messages: list[dict] = [
            {"role": "system", "content": BARE_SYSTEM_PROMPT},
            {"role": "user", "content": task_prompt},
        ]
        self._workdir.mkdir(parents=True, exist_ok=True)
        for iteration in range(1, max_iterations + 1):
            if time.monotonic() >= deadline:
                result.timed_out = True
                break
            reply: ChatReply = chat(self._cfg, messages)
            result.tokens_in += reply.prompt_tokens
            result.tokens_out += reply.completion_tokens
            claim = parse_claim(reply.content)
            if claim:
                result.turns.append(Turn(iteration, "claim", reply.content.strip()))
                result.claimed = claim
                result.claim_iteration = iteration
                break
            code = extract_code(reply.content)
            if code is None:
                result.turns.append(
                    Turn(iteration, "invalid", reply.content, "Reply with one ```python block.")
                )
                messages.append({"role": "assistant", "content": reply.content})
                messages.append(
                    {
                        "role": "user",
                        "content": "Your reply had no ```python block. Reply with exactly one "
                        "complete script, or CLAIM: SUCCESS / CLAIM: FAIL.",
                    }
                )
                continue
            exit_code, output, timed_out = self._execute(code, iteration)
            observation = f"exit={exit_code}\n{output}"
            result.turns.append(Turn(iteration, "script", code, observation, ok=exit_code == 0))
            if timed_out:
                result.timed_out = True
                break
            messages.append({"role": "assistant", "content": reply.content})
            messages.append(
                {
                    "role": "user",
                    "content": f"Script output:\n{observation}\n\nContinue, or CLAIM.",
                }
            )
        return result

    def _execute(self, code: str, iteration: int) -> tuple[int, str, bool]:
        script_path = self._workdir / f"iter-{iteration}.py"
        script_path.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, script_path.name],
                cwd=self._workdir,
                env={
                    **os.environ,
                    "IRONBENCH_OPS_PORT": self._port,
                    "PYTHONIOENCODING": "utf-8",
                },
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._iter_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            output = ""
            for stream in (e.stdout, e.stderr):
                if stream:
                    output += stream if isinstance(stream, str) else stream.decode("utf-8", "replace")
            return 124, _cap(output + "\n[script killed: iteration timeout]"), True
        combined = proc.stdout or ""
        if proc.stderr:
            combined += "\n[stderr]\n" + proc.stderr
        return proc.returncode, _cap(combined), False


class McpArm:
    """The tools arm: a real MCP server behind the model's native tool
    calling. Tools go to the API in OpenAI form (describing them in the
    prompt text makes gpt-oss end its turn inside the reasoning channel -
    observed live on LM Studio); each returned tool call is executed over
    the wire and its result goes back as a role=tool message. A text-JSON
    protocol remains as the fallback for models without native tool
    parsing."""

    def __init__(
        self,
        cfg: SolveConfig,
        *,
        client_factory: Any,
        allowed_families: tuple[str, ...],
    ) -> None:
        self._cfg = cfg
        self._client_factory = client_factory
        self._families = allowed_families

    def run(
        self,
        *,
        task_prompt: str,
        max_iterations: int,
        deadline: float,
        llm: Any = None,
    ) -> ArmResult:
        chat = llm or chat_with_retry
        result = ArmResult(turns=[], claimed=None, claim_iteration=None)
        client: McpWireClient = self._client_factory()
        try:
            catalog = [
                t for t in client.list_tools()
                if t.get("name", "").startswith(tuple(f + "_" for f in self._families))
            ]
            tools = native_tools(catalog)
            messages: list[dict] = [
                {"role": "system", "content": MCP_SYSTEM_PROMPT},
                {"role": "user", "content": task_prompt},
            ]
            for iteration in range(1, max_iterations + 1):
                if time.monotonic() >= deadline:
                    result.timed_out = True
                    break
                reply: ChatReply = chat(self._cfg, messages, tools=tools)
                result.tokens_in += reply.prompt_tokens
                result.tokens_out += reply.completion_tokens
                msg = reply.message or {}
                calls = msg.get("tool_calls") or []
                if calls:
                    messages.append(
                        {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
                    )
                    end = False
                    for tc in calls:
                        fn = tc.get("function") or {}
                        name = str(fn.get("name", ""))
                        arguments = parse_tool_arguments(fn.get("arguments"))
                        if name == "claim":
                            verdict = str(arguments.get("verdict", "")).upper()
                            result.turns.append(Turn(iteration, "claim", json.dumps(arguments)))
                            if verdict in ("SUCCESS", "FAIL"):
                                result.claimed = verdict
                                result.claim_iteration = iteration
                            end = True
                            continue
                        call = client.call_tool(name, arguments)
                        observation = (
                            f"TOOL {name} -> {'OK' if call['ok'] else 'ERROR'}\n{_cap(call['text'])}"
                        )
                        result.turns.append(
                            Turn(
                                iteration, "tool", json.dumps({"tool": name, "arguments": arguments}),
                                observation, ok=call["ok"],
                            )
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.get("id") or "", "content": _cap(call["text"])}
                        )
                    if end:
                        break
                    continue
                # text-JSON fallback (no native tool calls in the reply)
                text = reply.content
                payload = extract_last_json(text)
                if payload is None:
                    result.turns.append(
                        Turn(iteration, "invalid", text, "Call a tool, or call claim with your verdict.")
                    )
                    messages.append({"role": "assistant", "content": text})
                    messages.append(
                        {
                            "role": "user",
                            "content": "Reply by calling a tool, or call the claim tool "
                            "with verdict SUCCESS/FAIL.",
                        }
                    )
                    continue
                if "claim" in payload:
                    verdict = str(payload.get("claim", "")).upper()
                    result.turns.append(Turn(iteration, "claim", json.dumps(payload)))
                    if verdict in ("SUCCESS", "FAIL"):
                        result.claimed = verdict
                        result.claim_iteration = iteration
                    break
                name, arguments = payload.get("tool"), payload.get("arguments") or {}
                if not isinstance(name, str):
                    result.turns.append(Turn(iteration, "invalid", text, "Missing tool name."))
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": "Provide a tool name."})
                    continue
                call = client.call_tool(name, arguments)
                observation = (
                    f"TOOL {name} -> {'OK' if call['ok'] else 'ERROR'}\n{_cap(call['text'])}"
                )
                result.turns.append(
                    Turn(iteration, "tool", json.dumps({"tool": name, "arguments": arguments}),
                         observation, ok=call["ok"])
                )
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {"role": "user", "content": f"Tool result:\n{observation}\n\nContinue, or claim."}
                )
            return result
        finally:
            client.close()
