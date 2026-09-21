"""The ops A/B attempt runner (IH-105): preflight, seeded setup, arm loop,
state judge, metrics, restore.

An attempt is a pipeline with harness-owned boundaries:

1. preflight - identify the board by its boot output (two CH340s live on
   this bench; a floating COM number must never be trusted);
2. setup - deterministic seeded pre-state via harness transports, every
   step journaled;
3. the arm loop (ops_arms) - the agent operates the board inside the
   task's wall clock;
4. the state judge (ops_judge) - fresh connections, actual board state;
5. restore - best-effort path back to the healthy bench station, run
   even after failed attempts.

Metrics land in the per-attempt row: solved, claim vs judge verdict
(silent failure = SUCCESS claim the judge refutes), tokens, wall time,
journal coverage (journaled device operations / declared operations) and
rule-scanned accidents.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from io_core.esp_flash import DEFAULT_BOOTLOADER_OFFSET, EspFlasher
from io_core.journal import JsonlJournal
from io_core.mprepl import MpRepl
from io_core.serial_transport import SerialTransport
from ironbench.mcp_wire import McpWireError, spawn_mcp_client
from ironbench.ops_arms import JOURNALED_TOOL_RE, ArmResult, BareArm, McpArm, scan_bare_accidents
from ironbench.ops_assets import resolve_assets
from ironbench.ops_judge import OpsJudge, serial_factory
from ironbench.ops_tasks import OpsStep, OpsTask
from ironbench.runner_common import OUT_MARKER

BOOT_EVIDENCE_RE = re.compile(r"rst:0x|ets Jul|MicroPython|METEO BOOT|STATION |loads:", re.IGNORECASE)
SETUP_QUIET_SEC = 3.0


class OpsSetupError(RuntimeError):
    """A seeded setup step failed; the attempt is infra-invalid."""


def _new_run_dir(out_dir: Path, task_name: str) -> Path:
    """A fresh per-run artifact directory (same rationale as the firmware
    runner's _new_run_dir: a re-run must never inherit a previous run's
    artifacts)."""
    for _ in range(3):
        run_id = uuid.uuid4().hex[:12]
        run_dir = out_dir / task_name / f"run-{run_id}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:  # astronomically unlikely - just draw again
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / OUT_MARKER).write_text("ironbench ops artifacts\n", encoding="utf-8")
        return run_dir
    raise OSError(f"cannot create a unique run directory under {out_dir}")


# ---------------------------------------------------------------------------
# preflight


def ops_preflight(
    port: str,
    *,
    transport_factory: Any = None,
    seconds: float = 8.0,
) -> str:
    """Refuses to start an attempt against a port that shows no boot
    evidence. Returns the observed boot tail for the campaign log."""
    factory = transport_factory or serial_factory(port)
    try:
        t = factory()
        try:
            t.reset()
            deadline = time.monotonic() + seconds
            text = ""
            while time.monotonic() < deadline:
                in_waiting = getattr(t, "in_waiting", 0)
                data = t.read(int(in_waiting) if in_waiting else 256)
                if data:
                    text += data.decode("utf-8", "replace")
                time.sleep(0.05)
        finally:
            t.close()
    except (OSError, RuntimeError) as e:
        # TransportClosedError is a RuntimeError: a dead/unopenable port is
        # infra, classified the same as "no boot evidence"
        raise ConnectionError(
            f"preflight transport failure on {port}: {type(e).__name__}: {e}"
        ) from e
    if not BOOT_EVIDENCE_RE.search(text):
        raise ConnectionError(
            f"port {port} shows no boot evidence in {seconds}s - refusing to "
            "run ops against an unidentified port (is this the right CH340?)"
        )
    return text[-2000:]


# ---------------------------------------------------------------------------
# setup / restore steps


def _open_transport(port: str, transport_factory: Any = None) -> Any:
    if transport_factory is not None:
        return transport_factory()
    t = SerialTransport(port, timeout=1.0)
    t.open()
    return t


def _run_step(
    step: OpsStep,
    *,
    port: str,
    assets: dict[str, Path],
    journal: JsonlJournal,
    transport_factory: Any = None,
) -> str:
    kind, params = step.kind, step.params
    if kind == "wipe_flash":
        EspFlasher(on_event=journal).erase(port)
        return "flash erased"
    if kind == "flash_asset":
        path = assets[str(params["asset"])]
        EspFlasher(on_event=journal).flash(port, str(path), addr=DEFAULT_BOOTLOADER_OFFSET)
        return f"flashed {path.name}"
    if kind == "deploy_file":
        data = assets[str(params["asset"])].read_bytes()
        t = _open_transport(port, transport_factory)
        try:
            MpRepl(t).put_file(data, str(params["target"]))
        finally:
            t.close()
        return f"deployed {params['target']} ({len(data)}B)"
    if kind == "remove_file":
        t = _open_transport(port, transport_factory)
        try:
            try:
                MpRepl(t).exec_(f"import os; os.remove({str(params['path'])!r})")
            except Exception as e:
                if "ENOENT" not in str(e):
                    raise
        finally:
            t.close()
        return f"removed {params['path']}"
    raise ValueError(f"unknown step kind: {kind}")


def run_steps(
    steps: tuple[OpsStep, ...],
    *,
    port: str,
    assets: dict[str, Path],
    journal: JsonlJournal,
    phase: str,
    transport_factory: Any = None,
) -> list[str]:
    evidence = []
    for i, step in enumerate(steps):
        journal(f"harness_step_{phase}", {"index": i, "kind": step.kind, **step.params})
        try:
            note = _run_step(
                step, port=port, assets=assets, journal=journal,
                transport_factory=transport_factory,
            )
        except Exception as e:
            journal(
                f"harness_step_{phase}_failed",
                {"index": i, "kind": step.kind, "error": f"{type(e).__name__}: {e}"},
            )
            raise OpsSetupError(f"{phase} step {i} ({step.kind}) failed: {e}") from e
        journal(f"harness_step_{phase}_done", {"index": i, "result": note})
        evidence.append(note)
    return evidence


# ---------------------------------------------------------------------------
# dossier and metrics


def build_dossier(task: OpsTask, *, port: str, assets: dict[str, Path]) -> str:
    lines = []
    for line in task.dossier:
        text = line.replace("$PORT", port)
        for name, path in assets.items():
            text = text.replace(f"$ASSET_{name}", str(path))
        lines.append(text)
    return "\n".join(lines)


def build_task_prompt(task: OpsTask, *, port: str, assets: dict[str, Path]) -> str:
    parts = [f"Task: {task.description.strip()}"]
    dossier = build_dossier(task, port=port, assets=assets)
    if dossier:
        parts.append(f"Bench facts:\n{dossier}")
    if task.notes:
        parts.append("Expert notes from the bench team:\n" + "\n".join(f"- {n}" for n in task.notes))
    return "\n\n".join(parts)


def count_script_ops(scripts: list[str]) -> int:
    """State-changing operations declared by bare scripts (host-side serial
    opens, esptool runs)."""
    ops = 0
    for script in scripts:
        ops += len(re.findall(r"serial\.Serial\(", script))
        ops += len(re.findall(r"\besptool\b", script))
    return ops


def count_tool_ops(turns: list) -> int:
    return sum(
        1
        for t in turns
        if t.kind == "tool" and JOURNALED_TOOL_RE.match(json.loads(t.content).get("tool", ""))
    )


JOURNALED_KINDS = {
    "serial_open",
    "serial_write",
    "serial_read",
    "serial_reset",
    "serial_put",
    "serial_get",
    "esp_flash",
    "esp_erase",
    "file_write",
    "file_delete",
}


def count_journal_ops(journal_paths: list[Path]) -> int:
    ops = 0
    for path in journal_paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(event.get("kind", "")) in JOURNALED_KINDS:
                ops += 1
    return ops


def journal_coverage(declared_ops: int, journal_paths: list[Path]) -> float | None:
    """Journaled device operations / declared operations; None when the
    attempt declared none (nothing to cover)."""
    if declared_ops == 0:
        return None
    return min(1.0, count_journal_ops(journal_paths) / declared_ops)


# ---------------------------------------------------------------------------
# the attempt


@dataclasses.dataclass(frozen=True)
class OpsAttemptResult:
    task: str
    arm: str
    model: str
    attempt: int
    solved: bool
    claimed: str | None
    silent_failure: bool
    iterations: int
    duration_sec: float
    tokens_in: int
    tokens_out: int
    error: str | None
    error_kind: str  # none | infra | timeout
    judge: dict
    journal_coverage: float | None
    accidents: list[str]
    run_dir: Path

    def row(self) -> dict:
        data = dataclasses.asdict(self)
        data["run_dir"] = str(self.run_dir)
        return data


def run_ops_attempt(
    task: OpsTask,
    *,
    arm: str,
    model: str,
    attempt: int,
    port: str,
    out_dir: Path,
    llm_cfg: Any,
    allow_flash: bool = False,
    llm: Any = None,
    transport_factory: Any = None,
    mcp_client_factory: Any = None,
    preflight: bool = True,
) -> OpsAttemptResult:
    if arm not in ("bare", "mcp"):
        raise ValueError(f"unknown arm {arm!r}")
    if allow_flash:
        # the setup/restore steps flash and erase in THIS process (EspFlasher
        # reads the gate from the environment), same opt-in as the MCP arm's
        # server env gets below
        os.environ["IRONHARNESS_ALLOW_REAL_FLASH"] = "1"
    task_dir = task.source_dir or Path(".")
    started = time.monotonic()

    def finish(**kw) -> OpsAttemptResult:
        base = {
            "task": task.name,
            "arm": arm,
            "model": model,
            "attempt": attempt,
            "solved": False,
            "claimed": None,
            "silent_failure": False,
            "iterations": 0,
            "duration_sec": round(time.monotonic() - started, 1),
            "tokens_in": 0,
            "tokens_out": 0,
            "error": None,
            "error_kind": "none",
            "judge": {},
            "journal_coverage": None,
            "accidents": [],
            "run_dir": run_dir,
        }
        base.update(kw)
        result = OpsAttemptResult(**base)
        (run_dir / "result.json").write_text(
            json.dumps(result.row(), indent=2, default=str), encoding="utf-8"
        )
        return result

    run_dir = _new_run_dir(out_dir, task.name)
    journal = JsonlJournal(run_dir / "harness.jsonl", actor="ops-run")

    try:
        assets = resolve_assets(task, task_dir=task_dir, cache_dir=run_dir / "assets")
    except Exception as e:  # noqa: BLE001 - any asset failure is infra, recorded and returned
        journal("attempt_infra", {"stage": "assets", "error": str(e)})
        return finish(error=f"assets: {e}", error_kind="infra")

    if preflight:
        try:
            evidence = ops_preflight(port, transport_factory=transport_factory)
            (run_dir / "preflight.log").write_text(evidence, encoding="utf-8")
        except ConnectionError as e:
            journal("attempt_infra", {"stage": "preflight", "error": str(e)})
            return finish(error=str(e), error_kind="infra")

    try:
        run_steps(
            task.setup, port=port, assets=assets, journal=journal, phase="setup",
            transport_factory=transport_factory,
        )
    except OpsSetupError as e:
        _best_effort_restore(task, port=port, assets=assets, journal=journal,
                             transport_factory=transport_factory)
        return finish(error=str(e), error_kind="infra")

    prompt = build_task_prompt(task, port=port, assets=assets)
    deadline = time.monotonic() + task.wall_sec
    if arm == "bare":
        runner: Any = BareArm(
            llm_cfg, port=port, workdir=run_dir / "scripts",
            iter_timeout_sec=task.budget.iter_timeout_sec,
        )
    else:
        if mcp_client_factory is None:
            sandbox = run_dir / "sandbox"
            sandbox.mkdir(parents=True, exist_ok=True)
            env = {
                "IRONHARNESS_HOME": str(run_dir / "mcp-home"),
                "IRONHARNESS_SANDBOX": str(sandbox),
                # the server-side tool surface equals the prompt-side catalog:
                # a hallucinated call outside the task's families must fail at
                # the server gate, not only be missing from the prompt text
                "IRONHARNESS_ENABLED_KINDS": ",".join(sorted(task.allowed_tools)),
            }
            if allow_flash:
                env["IRONHARNESS_ALLOW_REAL_FLASH"] = "1"

            def mcp_client_factory() -> Any:
                return spawn_mcp_client(env)

        runner = McpArm(llm_cfg, client_factory=mcp_client_factory, allowed_families=task.allowed_tools)
    try:
        arm_result: ArmResult = runner.run(
            task_prompt=prompt, max_iterations=task.budget.max_iterations,
            deadline=deadline, llm=llm,
        )
    except (OSError, McpWireError) as e:
        # OSError covers URLError/TimeoutError/ConnectionRefusedError: the LLM
        # or the MCP server being down is infra, not an agent verdict
        journal("attempt_infra", {"stage": "arm", "error": f"{type(e).__name__}: {e}"})
        _best_effort_restore(task, port=port, assets=assets, journal=journal,
                             transport_factory=transport_factory)
        return finish(error=f"arm failed: {type(e).__name__}: {e}", error_kind="infra")

    (run_dir / "transcript.txt").write_text(arm_result.transcript(), encoding="utf-8")

    error_kind = "timeout" if arm_result.timed_out else "none"
    judge_report: dict = {}
    solved = False
    if arm_result.iterations > 0 and not arm_result.timed_out:
        judge = OpsJudge(
            port=None if transport_factory else port,
            transport_factory=transport_factory,
            transcript=arm_result.transcript(),
            assets=assets,
        )
        report = judge.run_task(task)
        solved = report.passed
        judge_report = {
            "passed": report.passed,
            "outcomes": [dataclasses.asdict(o) for o in report.outcomes],
        }
    (run_dir / "judge.json").write_text(json.dumps(judge_report, indent=2), encoding="utf-8")

    scripts = [t.content for t in arm_result.turns if t.kind == "script"]
    if arm == "bare":
        declared = count_script_ops(scripts)
        accidents = scan_bare_accidents(scripts, port)
        journal_paths = [run_dir / "harness.jsonl"]
    else:
        declared = count_tool_ops(arm_result.turns)
        accidents = []
        journal_paths = [run_dir / "harness.jsonl", run_dir / "mcp-home" / "journal.jsonl"]
    coverage = journal_coverage(declared, journal_paths)

    restore_error = _best_effort_restore(
        task, port=port, assets=assets, journal=journal, transport_factory=transport_factory
    )
    if restore_error is not None and error_kind == "none":
        error_kind = "infra"
    return finish(
        solved=solved,
        claimed=arm_result.claimed,
        silent_failure=arm_result.claimed == "SUCCESS" and not solved,
        iterations=arm_result.iterations,
        tokens_in=arm_result.tokens_in,
        tokens_out=arm_result.tokens_out,
        error=f"restore failed: {restore_error}" if restore_error else None,
        error_kind=error_kind,
        judge=judge_report,
        journal_coverage=coverage,
        accidents=accidents,
    )


def _best_effort_restore(
    task: OpsTask,
    *,
    port: str,
    assets: dict[str, Path],
    journal: JsonlJournal,
    transport_factory: Any = None,
) -> str | None:
    if not task.restore:
        return None
    try:
        run_steps(
            task.restore, port=port, assets=assets, journal=journal, phase="restore",
            transport_factory=transport_factory,
        )
    except OpsSetupError as e:
        return str(e)
    return None
