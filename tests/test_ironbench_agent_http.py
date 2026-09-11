"""HTTP layer of agent.chat (IH-30): until now the LLM in tests was always a
lambda, so the wire side - request shape, content extraction, typed errors -
had zero coverage. A local OpenAI-compatible http.server stands in for the
endpoint (chat() is allowed to talk to loopback by design)."""

from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ironbench.agent import SolveConfig, chat, solve_attempt
from ironbench.runner import ERROR_INFRA
from ironbench.tasks import load_task

TASKS_DIR = Path(__file__).resolve().parents[1] / "src" / "ironbench" / "tasks"
ANSWER = "```python\nprint('hello')\n```"


class FakeOpenAI:
    """Minimal OpenAI-compatible endpoint. behavior: 'ok' | 'http500' | 'badjson'."""

    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior
        self.requests: list[dict] = []
        handler = self._make_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(
                    {"body": body, "auth": self.headers.get("Authorization"), "path": self.path}
                )
                if outer.behavior == "http500":
                    self.send_response(500)
                    self.send_header("Content-Length", "4")
                    self.end_headers()
                    self.wfile.write(b"boom")
                    return
                if outer.behavior == "badjson":
                    data = b"this is not json at all"
                else:
                    data = json.dumps({"choices": [{"message": {"content": ANSWER}}]}).encode(
                        "utf-8"
                    )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass  # keep the test output clean

        return Handler

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"


def make_cfg(server: FakeOpenAI) -> SolveConfig:
    # the api key is a dummy echo value for the local endpoint (project convention)
    return SolveConfig(base_url=server.url, api_key="k", model="test-model", max_tokens=111)


def test_chat_roundtrip_request_shape_and_content():
    with FakeOpenAI() as srv:
        cfg = make_cfg(srv)
        content = chat(cfg, [{"role": "user", "content": "blink please"}])
    assert content == ANSWER  # the choices[0].message.content extraction
    assert len(srv.requests) == 1
    req = srv.requests[0]
    assert req["path"].endswith("/chat/completions")
    assert req["auth"] == "Bearer k"
    body = req["body"]
    assert body["model"] == "test-model"
    assert body["stream"] is False  # the loop cannot use streaming
    assert body["max_tokens"] == 111
    assert body["messages"] == [{"role": "user", "content": "blink please"}]
    assert "temperature" in body


def test_http_error_is_a_typed_domain_error():
    # a 500 surfaces as HTTPError (an OSError subclass the solve loop
    # classifies as an LLM-side infra failure), never as a weird crash
    with FakeOpenAI(behavior="http500") as srv, pytest.raises(urllib.error.HTTPError):
        chat(make_cfg(srv), [{"role": "user", "content": "hi"}])


def test_broken_json_is_a_typed_domain_error():
    with FakeOpenAI(behavior="badjson") as srv, pytest.raises(json.JSONDecodeError):
        chat(make_cfg(srv), [{"role": "user", "content": "hi"}])


def test_solve_attempt_real_http_error_is_infra(tmp_path):
    # end to end: a real 500 from a real socket flows through solve_attempt
    # as a clean LLM-side infra stop - one iteration, no runner call
    task = load_task(TASKS_DIR / "blink")

    def runner_must_not_be_called(task, *, out_dir, journal=None):
        raise AssertionError("the runner must not run on an LLM-side failure")

    with FakeOpenAI(behavior="http500") as srv:
        res = solve_attempt(
            task,
            make_cfg(srv),
            out_dir=tmp_path / "out",
            llm=chat,
            runner=runner_must_not_be_called,
        )
    assert not res.solved
    assert res.error_kind == ERROR_INFRA
    assert res.error is not None and res.error.startswith("LLM error")
    assert res.iterations == 1
