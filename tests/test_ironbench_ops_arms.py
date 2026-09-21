"""The two ops A/B arms (IH-105). Offline by construction: the LLM is
scripted, the bare arm's scripts run as real subprocesses (no board), and
the MCP arm talks to a fake client that records its calls. What is pinned:
the claim protocol, observation feedback, the iteration budget, and the
incident scan."""

import json
import time

from ironbench.agent import ChatReply, SolveConfig
from ironbench.ops_arms import (
    BareArm,
    McpArm,
    extract_last_json,
    native_tools,
    parse_claim,
    parse_tool_arguments,
    scan_bare_accidents,
)

CFG = SolveConfig(base_url="http://localhost:1234/v1", api_key="x", model="test-model")


def scripted_llm(replies):
    calls = []

    def llm(cfg, messages, tools=None):
        calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return replies.pop(0)

    llm.calls = calls
    return llm


# ---------------------------------------------------------------------------
# claim protocol / JSON extraction


def test_parse_claim_last_wins():
    text = "CLAIM: FAIL\n...thinking again...\nCLAIM: SUCCESS"
    assert parse_claim(text) == "SUCCESS"
    assert parse_claim("no claim here") is None
    assert parse_claim("CLAIM: MAYBE") is None


def test_extract_last_json_survives_braces_in_strings():
    text = '{"tool": "file_write", "arguments": {"content": "print(\x27{\x27)"}} ignore {"claim": "FAIL"}'
    payload = extract_last_json(text)
    assert payload == {"claim": "FAIL"}
    single = extract_last_json('{"tool": "a", "arguments": {"nested": {"x": 1}}}')
    assert single["tool"] == "a" and single["arguments"]["nested"] == {"x": 1}
    assert extract_last_json("no json at all") is None


# ---------------------------------------------------------------------------
# bare arm


def test_bare_arm_runs_script_then_claims(tmp_path):
    replies = [
        ChatReply(content="```python\nprint('probe ok')\n```", prompt_tokens=10, completion_tokens=5),
        ChatReply(content="CLAIM: SUCCESS", prompt_tokens=10, completion_tokens=2),
    ]
    arm = BareArm(CFG, port="COM9", workdir=tmp_path / "w", iter_timeout_sec=30)
    result = arm.run(
        task_prompt="probe the board", max_iterations=5,
        deadline=time.monotonic() + 60, llm=scripted_llm(replies),
    )
    assert result.claimed == "SUCCESS"
    assert result.claim_iteration == 2
    assert "probe ok" in result.turns[0].observation
    assert result.tokens_in == 20 and result.tokens_out == 7
    assert (tmp_path / "w" / "iter-1.py").is_file()


def test_bare_arm_claim_without_evidence_is_just_a_claim(tmp_path):
    # the arm never judges: a premature SUCCESS claim ends the attempt and
    # the STATE JUDGE (ops_run) refutes it later - that pairing is the
    # silent-failure metric
    replies = [ChatReply(content="CLAIM: SUCCESS")]
    arm = BareArm(CFG, port="COM9", workdir=tmp_path, iter_timeout_sec=10)
    result = arm.run(
        task_prompt="t", max_iterations=4, deadline=time.monotonic() + 30,
        llm=scripted_llm(replies),
    )
    assert result.claimed == "SUCCESS" and result.iterations == 1


def test_bare_arm_invalid_reply_gets_feedback(tmp_path):
    replies = [
        ChatReply(content="I would start by..."),
        ChatReply(content="CLAIM: FAIL"),
    ]
    arm = BareArm(CFG, port="COM9", workdir=tmp_path, iter_timeout_sec=10)
    result = arm.run(
        task_prompt="t", max_iterations=5, deadline=time.monotonic() + 30,
        llm=scripted_llm(replies),
    )
    assert result.turns[0].kind == "invalid"
    assert "```python" in result.turns[0].observation
    assert result.turns[1].kind == "claim"


def test_bare_arm_script_timeout_kills_the_attempt(tmp_path):
    replies = [ChatReply(content="```python\nimport time; time.sleep(30)\n```")]
    arm = BareArm(CFG, port="COM9", workdir=tmp_path, iter_timeout_sec=2)
    result = arm.run(
        task_prompt="t", max_iterations=3, deadline=time.monotonic() + 60,
        llm=scripted_llm(replies),
    )
    assert result.timed_out is True
    assert result.claimed is None
    assert "timeout" in result.turns[0].observation


def test_bare_arm_output_is_capped(tmp_path):
    replies = [ChatReply(content="```python\nprint('x' * 100000)\n```"), ChatReply(content="CLAIM: FAIL")]
    arm = BareArm(CFG, port="COM9", workdir=tmp_path, iter_timeout_sec=20)
    result = arm.run(
        task_prompt="t", max_iterations=2, deadline=time.monotonic() + 60,
        llm=scripted_llm(replies),
    )
    assert len(result.turns[0].observation) < 16 * 1024
    assert "truncated" in result.turns[0].observation


def test_bare_arm_wall_deadline_stops_before_new_iterations(tmp_path):
    replies = [ChatReply(content="CLAIM: SUCCESS")]
    arm = BareArm(CFG, port="COM9", workdir=tmp_path, iter_timeout_sec=5)
    result = arm.run(
        task_prompt="t", max_iterations=5, deadline=time.monotonic() - 1,
        llm=scripted_llm(replies),
    )
    assert result.timed_out and result.turns == []


def test_accident_scan_flags_wrong_port_and_open_without_timeout():
    scripts = [
        "import serial; s = serial.Serial('COM3', 115200)",  # not the assigned COM9
        "s = serial.Serial(port)\nwhile True:\n    s.read(1)",
    ]
    accidents = scan_bare_accidents(scripts, "COM9")
    assert any(a.startswith("wrong_port_reference:COM3") for a in accidents)
    assert "serial_open_without_timeout" in accidents
    assert scan_bare_accidents(["s = serial.Serial('COM9', timeout=1)"], "COM9") == []


# ---------------------------------------------------------------------------
# mcp arm


class FakeMcpClient:
    def __init__(self, tools, results=None):
        self._tools = tools
        self._results = results or {}
        self.calls = []
        self.closed = False

    def list_tools(self):
        return self._tools

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._results.get(name, {"ok": True, "text": "done"})

    def close(self):
        self.closed = True


TOOLS = [
    {"name": "file_write", "description": "write a sandbox file", "inputSchema": {"properties": {"path": {}}}},
    {"name": "serial_open", "description": "open serial", "inputSchema": {"properties": {"port": {}}}},
    {"name": "modbus_open", "description": "open modbus", "inputSchema": {"properties": {}}},
]


def _native_call(name: str, arguments: dict, call_id: str = "c1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}


def test_native_tools_adds_claim_and_keeps_schemas():
    tools = native_tools(TOOLS)
    names = [t["function"]["name"] for t in tools]
    assert names == ["file_write", "serial_open", "modbus_open", "claim"]
    assert tools[0]["function"]["parameters"] == TOOLS[0]["inputSchema"]
    assert tools[-1]["function"]["parameters"]["properties"]["verdict"]["enum"] == ["SUCCESS", "FAIL"]


def test_parse_tool_arguments_accepts_str_and_dict():
    assert parse_tool_arguments('{"port": "COM9"}') == {"port": "COM9"}
    assert parse_tool_arguments({"port": "COM9"}) == {"port": "COM9"}
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments("not json") == {}


def test_mcp_arm_filters_families_and_calls_tools_natively():
    replies = [
        ChatReply(
            content="",
            message={"tool_calls": [_native_call("serial_open", {"port": "COM9"})]},
            prompt_tokens=10,
            completion_tokens=4,
        ),
        ChatReply(
            content="",
            message={"tool_calls": [_native_call("claim", {"verdict": "SUCCESS"}, "c2")]},
        ),
    ]
    client = FakeMcpClient(TOOLS)
    arm = McpArm(CFG, client_factory=lambda: client, allowed_families=("serial", "file"))
    llm = scripted_llm(replies)
    result = arm.run(
        task_prompt="t", max_iterations=5, deadline=time.monotonic() + 60,
        llm=llm,
    )
    assert result.claimed == "SUCCESS"
    assert client.calls == [("serial_open", {"port": "COM9"})]
    assert client.closed is True
    # the claim tool never reaches the MCP server
    assert all(name != "claim" for name, _ in client.calls)
    # the tools went to the API, not into the prompt text
    assert all(c["tools"] for c in llm.calls)


def test_mcp_arm_tool_error_is_an_observation_not_a_crash():
    replies = [
        ChatReply(content="", message={"tool_calls": [_native_call("serial_open", {})]}),
        ChatReply(content="", message={"tool_calls": [_native_call("claim", {"verdict": "FAIL"}, "c2")]}),
    ]
    client = FakeMcpClient(TOOLS, results={"serial_open": {"ok": False, "text": "port busy"}})
    arm = McpArm(CFG, client_factory=lambda: client, allowed_families=("serial",))
    result = arm.run(
        task_prompt="t", max_iterations=5, deadline=time.monotonic() + 60,
        llm=scripted_llm(replies),
    )
    assert "ERROR" in result.turns[0].observation
    assert "port busy" in result.turns[0].observation
    assert result.claimed == "FAIL"


def test_mcp_arm_text_fallback_still_works():
    # models without native tool parsing reply with the text-JSON protocol
    replies = [
        ChatReply(content='{"tool": "serial_open", "arguments": {"port": "COM9"}}'),
        ChatReply(content='{"claim": "FAIL"}'),
    ]
    client = FakeMcpClient(TOOLS)
    arm = McpArm(CFG, client_factory=lambda: client, allowed_families=("serial",))
    result = arm.run(
        task_prompt="t", max_iterations=5, deadline=time.monotonic() + 60,
        llm=scripted_llm(replies),
    )
    assert client.calls == [("serial_open", {"port": "COM9"})]
    assert result.claimed == "FAIL"


def test_mcp_arm_garbage_reply_gets_feedback():
    replies = [ChatReply(content="Let me look around first."), ChatReply(content='{"claim": "FAIL"}')]
    client = FakeMcpClient(TOOLS)
    arm = McpArm(CFG, client_factory=lambda: client, allowed_families=("serial",))
    result = arm.run(
        task_prompt="t", max_iterations=5, deadline=time.monotonic() + 60,
        llm=scripted_llm(replies),
    )
    assert result.turns[0].kind == "invalid"
    assert "tool" in result.turns[0].observation


def test_mcp_arm_transcript_records_everything():
    replies = [
        ChatReply(content='{"tool": "serial_open", "arguments": {"port": "COM9"}}'),
        ChatReply(content='{"claim": "FAIL"}'),
    ]
    client = FakeMcpClient(TOOLS)
    arm = McpArm(CFG, client_factory=lambda: client, allowed_families=("serial",))
    result = arm.run(
        task_prompt="t", max_iterations=2, deadline=time.monotonic() + 60,
        llm=scripted_llm(replies),
    )
    text = result.transcript()
    assert "[tool]" in text and "serial_open" in text
    assert json.dumps({"port": "COM9"}) in text or '"port"' in text
