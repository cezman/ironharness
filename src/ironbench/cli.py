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
from ironbench.tasks import TASK_TARGETS, check_target_compat, load_tasks

DEFAULT_TASKS_DIR = Path(__file__).resolve().parent / "tasks"
SOLVE_DIR_NAME = "solve"


def _start_rows_campaign(rows_path: Path, header: dict) -> None:
    """Replaces any previous campaign's rows.jsonl with a one-line tombstone
    (same pattern as solve results.jsonl, audit D). Ops campaigns APPEND rows
    as attempts finish, so a repeated run into the same --campaign used to
    silently MIX the previous campaign's rows into the new one, and a run
    killed mid-way left the stale file indistinguishable from a complete
    campaign with fewer attempts (IH-122). After this call a torn campaign
    is visible as "tombstone + crash markers", never as someone else's old
    numbers.

    Two live campaigns writing the same rows.jsonl still collide (last
    writer wins per append) - one campaign per name at a time, like the
    solve path. A hard kill cannot run the crash handler, so that campaign
    is visible as the tombstone plus its completed rows only (no marker)."""
    for stale in sorted(rows_path.parent.glob(f"{rows_path.name}.*.tmp")):
        try:
            stale.unlink()
        except OSError:
            pass
    payload = json.dumps({"tombstone": True, **header}, ensure_ascii=False) + "\n"
    tmp = rows_path.with_name(f"{rows_path.name}.{os.getpid()}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    _replace_results_atomically(tmp, rows_path)


def _append_row(rows_path: Path, row: dict) -> None:
    with rows_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _crash_row(**ident) -> dict:
    """rows.jsonl record for an attempt/scenario the driver could not run to
    its own verdict (IH-122). error_kind=infra keeps it out of the judged
    pass rates; "crashed" is what makes a torn campaign tell itself apart
    from a complete one that simply made fewer attempts."""
    return {
        "crashed": True,
        "error_kind": "infra",
        "error": ident.pop("error"),
        **ident,
    }


def _cmd_ops_ab(args) -> int:
    from ironbench.ops_run import run_ops_attempt
    from ironbench.ops_tasks import load_ops_tasks

    if not args.allow_real:
        print(
            "refused: ops tasks erase flash and overwrite board files; "
            "pass --allow-real to confirm"
        )
        return 2
    if args.attempts < 1:
        print("--attempts must be >= 1")
        return 2
    tasks = {t.name: t for t in load_ops_tasks()}
    if args.task == "all":
        selected = list(tasks.values())
    elif args.task in tasks:
        selected = [tasks[args.task]]
    else:
        print(f"ops task not found: {args.task} (available: {', '.join(sorted(tasks))})")
        return 2
    cfg = resolve_llm_config()
    models = args.model or [cfg.model]
    arms = ["bare", "mcp"] if args.arm == "both" else [args.arm]
    campaign = args.campaign or time.strftime("%Y-%m-%d-%H%M%S", time.gmtime())
    campaign_dir = args.out / "ops" / f"{campaign}-{cfg.base_url.split('//')[-1].split(':')[0]}"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    rows_path = campaign_dir / "rows.jsonl"
    _start_rows_campaign(
        rows_path,
        {"campaign": campaign, "kind": "ops-ab", "models": models,
         "tasks": [t.name for t in selected], "attempts": args.attempts},
    )
    journal = JsonlJournal(campaign_dir / "journal.jsonl", actor="ops-ab")
    exit_code = 0
    for model in models:
        model_cfg = dataclasses.replace(cfg, model=model)
        for task in selected:
            for arm in arms:
                for attempt in range(1, args.attempts + 1):
                    print(f"[ops-ab] {task.name} {arm}/{model} attempt {attempt}...", flush=True)
                    try:
                        result = run_ops_attempt(
                            task,
                            arm=arm,
                            model=model,
                            attempt=attempt,
                            port=args.port,
                            out_dir=campaign_dir,
                            llm_cfg=model_cfg,
                            allow_flash=True,
                        )
                    except Exception as exc:  # noqa: BLE001 - one dead attempt must not kill the campaign
                        print(f"  crashed: {type(exc).__name__}: {exc}")
                        journal("attempt_crashed", {"error": str(exc)})
                        _append_row(rows_path, _crash_row(
                            task=task.name, arm=arm, model=model, attempt=attempt,
                            error=f"{type(exc).__name__}: {exc}",
                        ))
                        exit_code = 1
                        continue
                    journal("ops_attempt_result", result.row())
                    _append_row(rows_path, result.row())
                    verdict = "PASS" if result.solved else "FAIL"
                    print(
                        f"  {verdict} iter={result.iterations} claim={result.claimed} "
                        f"silent={result.silent_failure} tokens={result.tokens_in}/"
                        f"{result.tokens_out} ({result.duration_sec}s)"
                    )
                    if not result.solved:
                        exit_code = exit_code or 1
    print(f"rows: {rows_path}")
    return exit_code


def _cmd_ops_faults(args) -> int:
    from ironbench.ops_faults import FAULTS, FAULTS_BY_ID, detection_rate, run_fault_scenario
    from ironbench.ops_tasks import load_ops_task

    tasks_root = Path(__file__).resolve().parent / "ops"
    task_yaml = tasks_root / args.task / "task.yaml"
    if not task_yaml.is_file():
        print(f"ops task not found: {args.task} (under {tasks_root})")
        return 2
    task = load_ops_task(task_yaml)
    faults = list(FAULTS) if not args.fault else [FAULTS_BY_ID[f] for f in args.fault]
    arms = ["bare", "mcp"] if args.arm == "both" else [args.arm]
    matrix = [(f, a) for a in arms for f in faults]
    if args.dry_run:
        for f, a in matrix:
            print(f"  {args.task} {a} x fault {f.id}")
        return 0

    cfg = resolve_llm_config()
    models = args.model or [cfg.model]
    campaign = args.campaign or time.strftime("%Y-%m-%d-%H%M%S", time.gmtime())
    campaign_dir = args.out / "ops" / f"faults-{campaign}"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    rows_path = campaign_dir / "rows.jsonl"
    _start_rows_campaign(
        rows_path,
        {"campaign": campaign, "kind": "ops-faults", "models": models,
         "task": task.name, "arms": arms, "faults": [f.id for f in faults]},
    )
    rows: list[dict] = []
    exit_code = 0
    for model in models:
        model_cfg = dataclasses.replace(cfg, model=model)
        for arm in arms:
            for fault in faults:
                print(f"[ops-faults] {task.name} {arm}/{model} x {fault.id}...", flush=True)
                try:
                    row = run_fault_scenario(
                        fault,
                        task,
                        arm=arm,
                        llm_cfg=model_cfg,
                    )
                except Exception as exc:  # noqa: BLE001 - one dead scenario must not kill the suite
                    print(f"  crashed: {type(exc).__name__}: {exc}")
                    marker = _crash_row(
                        task=task.name, arm=arm, model=model, scenario=fault.id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    rows.append(marker)
                    _append_row(rows_path, marker)
                    exit_code = 1
                    continue
                row["model"] = model
                rows.append(row)
                _append_row(rows_path, row)
                print(f"  {row['outcome']} iter={row['iterations']} claim={row['claimed']}")
    # crashed scenarios carry no "detected" verdict - they are infra, not
    # missed detections, so they stay out of the rate
    rate = detection_rate([r for r in rows if not r.get("crashed")])
    print(f"detection rate: {rate:.0%}" if rate is not None else "detection rate: n/a")
    print(f"rows: {rows_path}")
    return exit_code


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
        "--allow-real",
        action="store_true",
        help="confirm wiping main.py on live boards for real-target tasks (audit B)",
    )
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
        "--allow-real",
        action="store_true",
        help="confirm wiping main.py on the live board for real-target tasks (audit B)",
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

    ops = sub.add_parser(
        "ops-ab",
        parents=[common],
        help="ops A/B: agent-operated board tasks, MCP tools vs bare scripts (IH-104/105)",
    )
    ops.add_argument("--task", required=True, help="ops task name or 'all'")
    ops.add_argument("--arm", choices=["bare", "mcp", "both"], default="both")
    ops.add_argument("--model", action="append", default=[], help="LLM model id (repeatable)")
    ops.add_argument("--attempts", type=int, default=2, help="attempts per task/arm/model")
    ops.add_argument("--port", required=True, help="serial port of the bench board")
    ops.add_argument(
        "--allow-real",
        action="store_true",
        help="confirm destructive board operations (flash erase / file wipes)",
    )
    ops.add_argument("--campaign", default=None, help="campaign name (default: UTC stamp)")

    flt = sub.add_parser(
        "ops-faults",
        parents=[common],
        help="fault-injection suite: offline seeded incidents x ops task (IH-106)",
    )
    flt.add_argument("--task", required=True, help="ops task name")
    flt.add_argument("--arm", choices=["bare", "mcp", "both"], default="both")
    flt.add_argument("--model", action="append", default=[], help="LLM model id (repeatable)")
    flt.add_argument("--fault", action="append", default=[], help="fault id (repeatable; default all)")
    flt.add_argument("--campaign", default=None, help="campaign name (default: UTC stamp)")
    flt.add_argument("--dry-run", action="store_true", help="print the matrix and exit")

    args = parser.parse_args(argv)

    if args.command == "ops-ab":
        return _cmd_ops_ab(args)

    if args.command == "ops-faults":
        return _cmd_ops_faults(args)

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
            # IH-38: load_task validated the NATIVE target; the override needs
            # a re-check, otherwise a plant task on unix runs with no detector.
            try:
                check_target_compat(task)
            except ValueError as e:
                print(f"refused: {e}")
                return 2
        # audit B: a real-target solve (native or overridden) wipes main.py on
        # the live board - refuse BEFORE any LLM token is spent
        if task.target == "real" and not args.allow_real:
            print(
                "refused: the real target wipes main.py on the live board; "
                "pass --allow-real to confirm"
            )
            return 2
        cfg = resolve_llm_config()
        if args.iterations is not None:
            if not 1 <= args.iterations <= 1000:
                # IH-112: refuse in the CLI's own style; SolveConfig would raise
                # anyway (its __post_init__ revalidates every replace)
                print("--iterations must be within 1..1000")
                return 2
            cfg = dataclasses.replace(cfg, max_iterations=args.iterations)
        solve_dir = args.out / SOLVE_DIR_NAME
        results = agent_solve_results(
            task, cfg, attempts=args.attempts, solve_dir=solve_dir, allow_real=True
        )
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
        # IH-38: same re-validation as solve - the override bypassed the
        # per-target rules from load_task.
        for t in selected:
            try:
                check_target_compat(t)
            except ValueError as e:
                print(f"refused: {e}")
                return 2
    # audit B (was IH-79, --all only): ANY real-target selection wipes main.py
    # on the live board - a named run is gated the same as the bulk one
    if not args.allow_real:
        real_names = [t.name for t in selected if t.target == "real"]
        if real_names:
            scope = "--all includes real tasks" if args.all and not args.task else (
                f"task {args.task!r} targets the live board"
            )
            print(
                f"refused: {scope} ("
                + ", ".join(real_names)
                + ") - staging wipes main.py on the board; pass --allow-real to confirm"
            )
            return 2

    with JsonlJournal(args.out / "journal.jsonl", actor="ironbench") as journal:
        all_passed = True
        for t in selected:
            res = run_task(t, out_dir=args.out, journal=journal, allow_real=args.allow_real)
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


def agent_solve_results(task, cfg, *, attempts: int, solve_dir: Path, allow_real: bool = False):
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
    # audit D (2026-09-20): a campaign that dies mid-run (SystemExit, Ctrl+C)
    # used to leave the PREVIOUS campaign's results.jsonl in place - the
    # report then aggregated stale rows as fresh data. A tombstone record is
    # atomically written BEFORE the first attempt: a torn campaign is visible
    # as "no rows" to the report, never as someone else's old numbers.
    tombstone = json.dumps(
        {
            "task": task.name,
            "tombstone": True,
            "model": cfg.model,
            "attempts": attempts,
            "notes": bool(task.notes),
        },
        ensure_ascii=False,
    )
    tombstone_tmp = results_path.with_name(f"{results_path.name}.{os.getpid()}.tmp")
    tombstone_tmp.write_text(tombstone + "\n", encoding="utf-8")
    _replace_results_atomically(tombstone_tmp, results_path)
    with JsonlJournal(solve_dir / "journal.jsonl", actor="ironbench") as journal:
        started = time.monotonic()
        results = agent_solve(
            task,
            cfg,
            attempts=attempts,
            out_dir=task_dir,
            journal=journal,
            allow_real=allow_real,
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
