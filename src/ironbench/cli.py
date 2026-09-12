"""ironbench CLI: `ironbench run/solve/list/report` - the firmware-agent benchmark."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sys
import time
from pathlib import Path

from io_core.journal import JsonlJournal
from ironbench import __version__
from ironbench.agent import resolve_llm_config
from ironbench.agent import solve as agent_solve
from ironbench.journal_view import write_view
from ironbench.publish import PublishError, publish_report
from ironbench.report import render_leaderboard, write_report
from ironbench.runner import clean_runs, run_task
from ironbench.tasks import TASK_TARGETS, load_tasks

DEFAULT_TASKS_DIR = Path(__file__).resolve().parent / "tasks"
SOLVE_DIR_NAME = "solve"


def _fmt_result(res) -> str:
    if res.passed:
        return f"PASS {res.task} ({res.duration_sec}s)"
    reasons = []
    if res.missed:
        reasons.append(f"not found in serial: {', '.join(repr(p) for p in res.missed)}")
    if res.hit_fail:
        reasons.append(f"forbidden output: {', '.join(repr(p) for p in res.hit_fail)}")
    if res.error:
        reasons.append(res.error)
    if res.exit_code not in (0, None):
        reasons.append(f"exit={res.exit_code}")
    return f"FAIL {res.task} ({res.duration_sec}s): {'; '.join(reasons)}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ironbench", description="Firmware-agent benchmark")
    parser.add_argument("--version", action="version", version=f"ironbench {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR)
    common.add_argument("--out", type=Path, default=Path(".ironbench"), help="run logs directory")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="run tasks (reference solutions)")
    run.add_argument("--task", help="task name (e.g. blink)")
    run.add_argument("--all", action="store_true", help="run all tasks")
    run.add_argument(
        "--target",
        choices=TASK_TARGETS,
        default=None,
        help="override the target (e.g. --target unix: free local run)",
    )
    run.add_argument(
        "--serial", action="store_true", help="print the tail of the serial log after each task"
    )

    solve_p = sub.add_parser("solve", parents=[common], help="an LLM agent solves the task")
    solve_p.add_argument("--task", required=True, help="task name")
    solve_p.add_argument("--attempts", type=int, default=1, help="attempts per task (k)")
    solve_p.add_argument(
        "--iterations", type=int, default=None, help="iteration limit per attempt"
    )
    solve_p.add_argument(
        "--target",
        choices=TASK_TARGETS,
        default=None,
        help="override the target (e.g. --target unix: a campaign with no Wokwi quota)",
    )

    rep = sub.add_parser("report", parents=[common], help="pass@k report over solve campaigns")
    rep.add_argument(
        "--solve-dir", type=Path, default=None, help="campaigns directory (default <out>/solve)"
    )
    rep.add_argument(
        "--publish",
        action="store_true",
        help="also push the leaderboard (index.html + data.json) to the pages branch",
    )
    rep.add_argument(
        "--remote", default="origin", help="git remote for --publish (default origin)"
    )
    rep.add_argument(
        "--pages-branch", default="gh-pages", help="branch for --publish (default gh-pages)"
    )

    sub.add_parser("list", parents=[common], help="list available tasks")

    clean = sub.add_parser(
        "clean",
        parents=[common],
        help="remove old per-run artifact dirs (only in ironbench-marked out dirs)",
    )
    clean.add_argument(
        "--keep", type=int, default=1, help="run dirs to keep per task (default 1)"
    )

    jv = sub.add_parser(
        "journal",
        parents=[common],
        help="self-contained HTML timeline of a JSONL operation journal",
    )
    jv.add_argument(
        "journal",
        nargs="?",
        type=Path,
        default=None,
        help="journal file (default <out>/journal.jsonl)",
    )
    jv.add_argument(
        "--out-file",
        type=Path,
        default=None,
        help="output HTML (default next to the journal: <journal>.view.html)",
    )

    args = parser.parse_args(argv)
    tasks = load_tasks(args.tasks_dir)

    if args.command == "list":
        for t in tasks:
            meta = ", ".join(t.tags) + (f" · level {t.level}" if t.level else "")
            print(f"{t.name}" + (f" [{meta}]" if meta else "") + f": {t.description}")
        return 0

    if args.command == "report":
        solve_dir = args.solve_dir or (args.out / SOLVE_DIR_NAME)
        task_meta = {
            t.name: {"tags": list(t.tags), "level": t.level} for t in tasks
        }
        json_path, html_path = write_report(solve_dir, args.out, task_meta)
        report = json.loads(json_path.read_text(encoding="utf-8"))
        for g in report["groups"]:
            status = "PASS" if g["passed"] else "FAIL"
            print(
                f"{status} {g['model']} / {g['task']}: {g['solved']}/{g['attempts']}"
                f" (avg iterations: {g['avg_iterations']})"
            )
        for model, cells in report.get("class_profile", {}).items():
            parts = [f"{tag} {c['solved']}/{c['attempts']}" for tag, c in cells.items()]
            print(f"profile {model}: " + (", ".join(parts) if parts else "-"))
        print(f"pass@k: {report['pass_at_k']:.0%}")
        print(f"reports: {json_path} and {html_path}")
        if args.publish:
            try:
                sha = publish_report(
                    report,
                    render_leaderboard(report),
                    repo=Path.cwd(),
                    remote=args.remote,
                    branch=args.pages_branch,
                )
            except (PublishError, OSError) as exc:
                print(f"publish failed: {exc}")
                return 1
            print(f"published: {args.remote}/{args.pages_branch} @ {sha[:12]}")
        return 0

    if args.command == "solve":
        task = next((t for t in tasks if t.name == args.task), None)
        if task is None:
            print(f"task not found: {args.task} (available: {', '.join(t.name for t in tasks)})")
            return 2
        if args.attempts < 1:
            print("--attempts must be >= 1")
            return 2
        if args.target:
            task = dataclasses.replace(task, target=args.target)
        cfg = resolve_llm_config()
        if args.iterations is not None:
            cfg = dataclasses.replace(cfg, max_iterations=args.iterations)
        solve_dir = args.out / SOLVE_DIR_NAME
        results = agent_solve_results(task, cfg, attempts=args.attempts, solve_dir=solve_dir)
        # pass@k semantics: the campaign succeeds if the task was solved by at least one attempt
        return 0 if any(r.solved for r in results) else 1

    if args.command == "clean":
        if args.keep < 0:
            print("--keep must be >= 0")
            return 2
        try:
            removed = clean_runs(args.out, keep=args.keep)
        except ValueError as exc:
            print(f"clean refused: {exc}")
            return 2
        print(f"removed {removed} run dir(s) under {args.out} (kept {args.keep} per task)")
        return 0

    if args.command == "journal":
        journal_path = args.journal or (args.out / "journal.jsonl")
        if not journal_path.is_file():
            print(f"journal not found: {journal_path}")
            return 2
        out_file = args.out_file or journal_path.with_name(journal_path.name + ".view.html")
        try:
            _, view = write_view(journal_path, out_file)
        except ValueError as exc:
            print(f"journal view refused: {exc}")
            return 2
        notes = []
        if view["skipped_lines"]:
            notes.append(f"{view['skipped_lines']} unparseable line(s) skipped")
        if view["omitted_events"]:
            notes.append(f"{view['omitted_events']} middle event(s) omitted")
        print(
            f"journal view: {view['shown_events']} event(s) shown"
            + (f" ({'; '.join(notes)})" if notes else "")
            + f" -> {out_file}"
        )
        return 0

    # run
    if args.task:
        selected = [t for t in tasks if t.name == args.task]
        if not selected:
            print(f"task not found: {args.task} (available: {', '.join(t.name for t in tasks)})")
            return 2
    elif args.all:
        selected = tasks
    else:
        print("specify --task <name> or --all")
        return 2
    if args.target:
        selected = [dataclasses.replace(t, target=args.target) for t in selected]

    with JsonlJournal(args.out / "journal.jsonl", actor="ironbench") as journal:
        all_passed = True
        for t in selected:
            res = run_task(t, out_dir=args.out, journal=journal)
            print(_fmt_result(res))
            if args.serial and res.serial_log:
                lines = res.serial_log.read_text(encoding="utf-8", errors="replace").splitlines()
                print("    " + "\n    ".join(lines[-12:]))
            all_passed = all_passed and res.passed
    return 0 if all_passed else 1


def _replace_results_atomically(tmp_path: Path, results_path: Path) -> None:
    """os.replace with a bounded retry: on Windows a concurrent reader holding
    an open handle (a report run, an indexer, an antivirus) makes the rename
    fail with PermissionError. Without a retry the campaign would die AFTER
    burning all its attempts and results.jsonl would silently keep the
    PREVIOUS campaign's data - the exact dishonesty IH-31 exists to prevent.
    Retries give the reader time to finish; exhaustion raises loudly (the
    journal still has every attempt_result)."""
    delay = 0.05
    for attempt in range(6):
        try:
            os.replace(tmp_path, results_path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def clean_stale_attempts(task_dir: Path) -> int:
    """Removes attempt-* artifact directories left by a PREVIOUS campaign on
    the task (IH-31): a shorter re-run must not leave a longer run's iter-*
    files behind, or the artifacts lie about what the model produced. Also
    removes stale results.jsonl.*.tmp files (a process killed between the
    write and the replace would otherwise leave them forever). Called before
    the attempts start. Two concurrent campaigns on the SAME task still
    collide (out of scope: one campaign per task at a time)."""
    removed = 0
    if not task_dir.is_dir():
        return 0
    for stale in sorted(task_dir.glob("attempt-*")):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
            removed += 1
    for stale_tmp in sorted(task_dir.glob("results.jsonl.*.tmp")):
        try:
            stale_tmp.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def agent_solve_results(task, cfg, *, attempts: int, solve_dir: Path):
    """A solve campaign: attempts + writing results.jsonl + progress printing.

    results.jsonl lives in the task directory and is replaced atomically
    (tmp + os.replace, IH-31): a repeated campaign on the same task replaces
    its results in the report instead of duplicating them, and a concurrent
    reader/writer can never observe a half-written file (last campaign wins,
    by design). Stale attempt artifacts of a longer previous campaign are
    cleaned before the run.
    """
    task_dir = solve_dir / task.name
    task_dir.mkdir(parents=True, exist_ok=True)
    clean_stale_attempts(task_dir)
    results_path = task_dir / "results.jsonl"
    with JsonlJournal(solve_dir / "journal.jsonl", actor="ironbench") as journal:
        started = time.monotonic()
        results = agent_solve(
            task, cfg, attempts=attempts, out_dir=task_dir, journal=journal
        )
        payload = "".join(
            json.dumps(
                {
                    "task": r.task,
                    "attempt": r.attempt,
                    "solved": r.solved,
                    "iterations": r.iterations,
                    "model": cfg.model,
                    "duration_sec": r.duration_sec,
                    "error": r.error,
                    "error_kind": r.error_kind,
                    # benchmark honesty (IH-21): whether the agent's prompt
                    # carried the task's expert notes
                    "notes": bool(task.notes),
                },
                ensure_ascii=False,
            )
            + "\n"
            for r in results
        )
        tmp_path = results_path.with_name(f"{results_path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        _replace_results_atomically(tmp_path, results_path)
    for r in results:
        status = "SOLVED" if r.solved else "not solved"
        print(
            f"attempt {r.attempt}: {status} in {r.iterations} iterations ({r.duration_sec}s)"
            + (f" - {r.error}" if r.error and not r.solved else "")
        )
    solved_count = sum(1 for r in results if r.solved)
    print(
        f"summary {task.name} [{cfg.model}]: {solved_count}/{attempts}"
        f" in {round(time.monotonic() - started, 1)}s, artifacts: {task_dir}"
    )
    return results


if __name__ == "__main__":
    sys.exit(main())
