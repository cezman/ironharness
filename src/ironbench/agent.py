"""ironbench agent loop (2.4): an LLM writes main.py for the task, the runner scores it.

A minimal loop without function calling: the agent receives the task statement and
the latest serial output, answers with a single ```python block containing the full
main.py code; the code is executed by the runner (REPL-paste in Wokwi) and the result
comes back as feedback. This way the loop works with any OpenAI-compatible model,
including local LM Studio/ollama without tool-calling support.

Config - environment variables (or .env): LLM_BASE_URL (defaults to local
LM Studio), LLM_API_KEY (a dummy value is fine for local servers), LLM_MODEL.

Outbound request safety: the scheme must be http/https, redirects are forbidden,
link-local and cloud metadata hosts are always blocked, and private/loopback
addresses are allowed only with the LLM_ALLOW_LOCAL=1 flag (on by default - the
project targets a local LLM; turning it off leaves only public endpoints).
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ironbench.runner import (
    ERROR_INFRA,
    ERROR_NONE,
    ERROR_RUN,
    find_env_file,
    is_infra_error,
    load_env_file,
    run_task,
)
from ironbench.tasks import Task

SYSTEM_PROMPT = (
    "You are an embedded engineer. You write MicroPython firmware for ESP32 as a single "
    "top-level script. Reply with exactly one ```python block containing the full "
    "main.py code, no explanations."
)

PLANT_SYSTEM_PROMPT = (
    "You are a control-systems engineer. You write a Python controller as a function "
    "control(t, y, setpoint). Reply with exactly one ```python block containing the full "
    "main.py code, no explanations."
)

CODE_FENCE = re.compile(r"```(?:python|micropython)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# The file the agent writes (in golden tasks this slot is taken by solution.py)
AGENT_FILE = "main.py"

# How many lines of serial output to show the agent as feedback
SERIAL_FEEDBACK_LINES = 40

# Always-blocked destinations (cloud metadata) - even with LLM_ALLOW_LOCAL=1
ALWAYS_BLOCKED_IPS = frozenset({ipaddress.ip_address("169.254.169.254")})


@dataclasses.dataclass(frozen=True)
class SolveConfig:
    base_url: str
    api_key: str
    model: str
    max_iterations: int = 5
    temperature: float = 0.7
    timeout_sec: int = 600
    allow_local: bool = True
    # generation ceiling: without it, reasoning models on hard tasks get stuck
    # "thinking" forever and hog the server queue
    max_tokens: int = 8192


def _env_map() -> dict[str, str]:
    env_file = find_env_file()
    return load_env_file(env_file) if env_file else {}


def _pick(explicit: str | None, names: tuple[str, ...], default: str) -> str:
    if explicit:
        return explicit
    env = _env_map()
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
        if env.get(name):
            return env[name]
    return default


def resolve_llm_config(
    base_url: str | None = None, api_key: str | None = None, model: str | None = None
) -> SolveConfig:
    """LLM config: explicit arguments > environment > .env > defaults."""
    return SolveConfig(
        base_url=_pick(base_url, ("LLM_BASE_URL",), "http://localhost:1234/v1"),
        api_key=_pick(api_key, ("LLM_API_KEY",), "lm-studio"),
        model=_pick(model, ("LLM_MODEL",), "qwen3.5-9b"),
        max_iterations=int(_pick(None, ("LLM_MAX_ITERATIONS",), "5") or 5),
        # local 9B models with long context think for minutes per call
        timeout_sec=int(_pick(None, ("LLM_TIMEOUT",), "600") or 600),
        allow_local=_pick(None, ("LLM_ALLOW_LOCAL",), "1").strip().lower() not in ("0", "false", "no"),
        max_tokens=int(_pick(None, ("LLM_MAX_TOKENS",), "8192") or 8192),
    )


def validate_endpoint(base_url: str, *, allow_local: bool) -> None:
    """SSRF boundary: scheme, host resolution, metadata/link-local blocked, and
    (optionally) private networks."""
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"LLM_BASE_URL must be http/https: {base_url}")
    host = (urllib.parse.urlsplit(base_url).hostname or "").rstrip(".")
    if not host:
        raise ValueError(f"LLM_BASE_URL has no host: {base_url}")
    if host.lower() in ("metadata.google.internal", "metadata"):
        raise ValueError(f"blocked metadata host: {host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"LLM_BASE_URL host does not resolve: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # leave is_reserved alone: for IPv6 it also matches ::1
        if ip in ALWAYS_BLOCKED_IPS or ip.is_link_local or ip.is_multicast:
            raise ValueError(f"blocked address {host} -> {ip}")
        if not allow_local and (ip.is_private or ip.is_loopback or ip.is_unspecified):
            raise ValueError(
                f"private/loopback address {host} -> {ip} is forbidden with LLM_ALLOW_LOCAL=0"
            )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are forbidden: the URL is checked before the request; a
    redirection would bypass the boundary."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def chat(cfg: SolveConfig, messages: list[dict]) -> str:
    """A single /chat/completions call without an SDK (urllib suffices for a local server)."""
    validate_endpoint(cfg.base_url, allow_local=cfg.allow_local)
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    with _OPENER.open(req, timeout=cfg.timeout_sec) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"] or ""


def extract_code(response: str) -> str | None:
    """The last ```python block in the response; None when there are no blocks."""
    blocks = CODE_FENCE.findall(response)
    if not blocks:
        return None
    code = blocks[-1].strip()
    return code + "\n" if code else None


def _first_prompt(task: Task) -> str:
    if task.target == "plant":
        return (
            f"Task: {task.description}\n\n"
            f"Write the complete code of the {AGENT_FILE} file - the controller of the "
            "closed-loop system as a control(t, y, setpoint) function. "
            "The answer must be a single ```python block with the complete code."
        )
    return (
        f"Task: {task.description}\n\n"
        f"Write the complete code of the {AGENT_FILE} file for MicroPython ESP32. "
        "Printing to serial is a regular print(). "
        "The answer must be a single ```python block with the complete code."
    )


def _serial_feedback(serial_log: Path | None) -> str:
    if serial_log is None or not serial_log.is_file():
        return "(serial output is empty - the firmware did not start)"
    lines = serial_log.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-SERIAL_FEEDBACK_LINES:])


def _work_task(task: Task, work_dir: Path) -> Task:
    """A copy of the task in the working directory: entry is the agent's main.py, no solution.py."""
    work_dir.mkdir(parents=True, exist_ok=True)
    for item in task.directory.iterdir():
        if item.is_file() and item.name not in ("solution.py", "task.yaml"):
            (work_dir / item.name).write_bytes(item.read_bytes())
    return dataclasses.replace(task, directory=work_dir, entry=AGENT_FILE)


@dataclasses.dataclass(frozen=True)
class AttemptResult:
    task: str
    attempt: int
    solved: bool
    iterations: int
    duration_sec: float
    work_dir: Path
    error: str | None = None
    # structured classification of WHY the attempt ended the way it did (IH-22),
    # same vocabulary as TaskResult.error_kind: "none" (solved, or the last run
    # was clean but the checks missed), "infra" (environment/LLM-server failure),
    # "timeout", "run" (a verdict on the agent's code - crash, cheat, or the LLM
    # never produced runnable code). Machine-readable for results.jsonl and the
    # report - consumers must not parse the free-form error text.
    error_kind: str = ERROR_NONE


def solve_attempt(
    task: Task,
    cfg: SolveConfig,
    *,
    attempt: int = 1,
    out_dir: Path,
    llm=chat,
    runner=run_task,
    journal=None,
) -> AttemptResult:
    """One attempt to solve the task: the "LLM answer -> main.py -> run -> feedback" loop.

    llm/runner are injection points for offline tests (a fake LLM and runner).
    """
    attempt_dir = out_dir / f"attempt-{attempt}"
    work_dir = attempt_dir / "work"
    work_task = _work_task(task, work_dir)

    messages: list[dict] = [
        {"role": "system", "content": PLANT_SYSTEM_PROMPT if task.target == "plant" else SYSTEM_PROMPT},
        {"role": "user", "content": _first_prompt(task)},
    ]
    start = time.monotonic()
    solved = False
    error: str | None = None
    iterations = 0
    llm_failed = False  # the loop stopped on the LLM side, not on a run verdict
    run_kind: str | None = None  # error_kind of the last runner verdict; None = never ran
    while iterations < cfg.max_iterations:
        iterations += 1
        try:
            response = llm(cfg, messages)
        except (OSError, ValueError, LookupError, TypeError) as e:
            # network/HTTP/broken LLM server response (incl. an empty "choices") -
            # an attempt error, not a runner crash; for the agent this is
            # environment-level (it cannot fix the server)
            error = f"LLM error: {e}"
            llm_failed = True
            break
        code = extract_code(response)
        if code is None:
            messages.append({"role": "assistant", "content": response})
            messages.append(
                {
                    "role": "user",
                    "content": "The answer has no ```python block. Try again: exactly one "
                    "```python block with the full main.py code.",
                }
            )
            continue
        (work_dir / AGENT_FILE).write_text(code, encoding="utf-8")
        if journal:
            journal("iteration", {"task": task.name, "attempt": attempt, "n": iterations})
        result = runner(work_task, out_dir=attempt_dir, journal=journal)
        run_kind = result.error_kind
        # iteration artifacts: the code and serial output are saved before the next move overwrites them
        (attempt_dir / f"iter-{iterations}.main.py").write_text(code, encoding="utf-8")
        if result.serial_log and result.serial_log.is_file():
            (attempt_dir / f"iter-{iterations}.serial.log").write_bytes(
                result.serial_log.read_bytes()
            )
        if result.passed:
            solved = True
            break
        if is_infra_error(result):
            # the environment is broken (no Renode/firmware/CLI) - the LLM cannot fix
            # it, further iterations would only burn tokens; the structured
            # error_kind comes from the runner, never from parsing firmware/CLI text
            error = f"environment not ready, attempt stopped: {result.error}"
            break
        messages.append({"role": "assistant", "content": response})
        messages.append(
            {
                "role": "user",
                "content": "The check failed."
                + (f" Reason: {result.error}" if result.error else "")
                + "\n\nRun output:\n\n"
                f"{_serial_feedback(result.serial_log)}\n\n"
                "Fix the code and again send exactly one ```python block with the full main.py.",
            }
        )
    if error is None and not solved:
        error = f"iteration limit ({cfg.max_iterations}) exhausted"

    # Attempt-level classification (IH-22): a loop-side LLM failure is infra;
    # otherwise the last runner verdict carries (including "none" - the
    # firmware ran clean but the checks did not pass); a failure with no
    # verdict at all (the LLM never produced runnable code) is a run-level
    # fault of the agent's output.
    if solved:
        error_kind = ERROR_NONE
    elif llm_failed:
        error_kind = ERROR_INFRA
    elif run_kind is not None:
        error_kind = run_kind
    else:
        error_kind = ERROR_RUN

    duration = round(time.monotonic() - start, 2)
    return AttemptResult(
        task=task.name,
        attempt=attempt,
        solved=solved,
        iterations=iterations,
        duration_sec=duration,
        work_dir=work_dir,
        error=error,
        error_kind=error_kind,
    )


def solve(
    task: Task,
    cfg: SolveConfig,
    *,
    attempts: int = 1,
    out_dir: Path,
    llm=chat,
    runner=run_task,
    journal=None,
) -> list[AttemptResult]:
    """A pass@k campaign: attempts independent tries at solving the task."""
    results = [
        solve_attempt(
            task, cfg, attempt=n, out_dir=out_dir, llm=llm, runner=runner, journal=journal
        )
        for n in range(1, attempts + 1)
    ]
    if journal:
        for r in results:
            journal(
                "attempt_result",
                {
                    "task": r.task,
                    "attempt": r.attempt,
                    "solved": r.solved,
                    "iterations": r.iterations,
                    "model": cfg.model,
                    "duration_sec": r.duration_sec,
                    "error": r.error,
                    "error_kind": r.error_kind,
                },
            )
    return results
