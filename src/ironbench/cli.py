"""CLI ironbench: `ironbench run/solve/list/report` — бенчмарк firmware-агентов."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

from io_core.journal import JsonlJournal
from ironbench import __version__
from ironbench.agent import resolve_llm_config
from ironbench.agent import solve as agent_solve
from ironbench.report import write_report
from ironbench.runner import run_task
from ironbench.tasks import load_tasks

DEFAULT_TASKS_DIR = Path(__file__).resolve().parent / "tasks"
SOLVE_DIR_NAME = "solve"


def _fmt_result(res) -> str:
    if res.passed:
        return f"PASS {res.task} ({res.duration_sec}s)"
    reasons = []
    if res.missed:
        reasons.append(f"не найдено в serial: {', '.join(repr(p) for p in res.missed)}")
    if res.hit_fail:
        reasons.append(f"запрещённый вывод: {', '.join(repr(p) for p in res.hit_fail)}")
    if res.error:
        reasons.append(res.error)
    if res.exit_code not in (0, None):
        reasons.append(f"exit={res.exit_code}")
    return f"FAIL {res.task} ({res.duration_sec}s): {'; '.join(reasons)}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ironbench", description="Бенчмарк firmware-агентов")
    parser.add_argument("--version", action="version", version=f"ironbench {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR)
    common.add_argument("--out", type=Path, default=Path(".ironbench"), help="каталог логов прогона")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="запустить задачи (эталонные)")
    run.add_argument("--task", help="имя задачи (например, blink)")
    run.add_argument("--all", action="store_true", help="запустить все задачи")
    run.add_argument(
        "--serial", action="store_true", help="печатать хвост serial-лога после задачи"
    )

    solve_p = sub.add_parser("solve", parents=[common], help="LLM-агент решает задачу")
    solve_p.add_argument("--task", required=True, help="имя задачи")
    solve_p.add_argument("--attempts", type=int, default=1, help="попыток на задачу (k)")
    solve_p.add_argument(
        "--iterations", type=int, default=None, help="лимит итераций на попытку"
    )

    rep = sub.add_parser("report", parents=[common], help="отчёт pass@k по solve-кампаниям")
    rep.add_argument(
        "--solve-dir", type=Path, default=None, help="каталог кампаний (по умолчанию <out>/solve)"
    )

    sub.add_parser("list", parents=[common], help="показать доступные задачи")

    args = parser.parse_args(argv)
    tasks = load_tasks(args.tasks_dir)

    if args.command == "list":
        for t in tasks:
            print(f"{t.name}: {t.description}")
        return 0

    if args.command == "report":
        solve_dir = args.solve_dir or (args.out / SOLVE_DIR_NAME)
        json_path, html_path = write_report(solve_dir, args.out)
        report = json.loads(json_path.read_text(encoding="utf-8"))
        for g in report["groups"]:
            status = "PASS" if g["passed"] else "FAIL"
            print(
                f"{status} {g['model']} / {g['task']}: {g['solved']}/{g['attempts']}"
                f" (в среднем итераций: {g['avg_iterations']})"
            )
        print(f"pass@k: {report['pass_at_k']:.0%}")
        print(f"отчёты: {json_path} и {html_path}")
        return 0

    if args.command == "solve":
        task = next((t for t in tasks if t.name == args.task), None)
        if task is None:
            print(f"задача не найдена: {args.task} (доступно: {', '.join(t.name for t in tasks)})")
            return 2
        if args.attempts < 1:
            print("--attempts должен быть >= 1")
            return 2
        cfg = resolve_llm_config()
        if args.iterations is not None:
            cfg = dataclasses.replace(cfg, max_iterations=args.iterations)
        solve_dir = args.out / SOLVE_DIR_NAME
        results = agent_solve_results(task, cfg, attempts=args.attempts, solve_dir=solve_dir)
        # семантика pass@k: успех кампании — задача решена хотя бы одной попыткой
        return 0 if any(r.solved for r in results) else 1

    # run
    if args.task:
        selected = [t for t in tasks if t.name == args.task]
        if not selected:
            print(f"задача не найдена: {args.task} (доступно: {', '.join(t.name for t in tasks)})")
            return 2
    elif args.all:
        selected = tasks
    else:
        print("укажите --task <имя> или --all")
        return 2

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


def agent_solve_results(task, cfg, *, attempts: int, solve_dir: Path):
    """Кампания solve: попытки + запись results.jsonl + печать прогресса.

    results.jsonl живёт в каталоге задачи и перезаписывается: повторная кампания
    по той же задаче заменяет её результаты в отчёте, а не дублирует.
    """
    task_dir = solve_dir / task.name
    task_dir.mkdir(parents=True, exist_ok=True)
    results_path = task_dir / "results.jsonl"
    with JsonlJournal(solve_dir / "journal.jsonl", actor="ironbench") as journal:
        started = time.monotonic()
        results = agent_solve(
            task, cfg, attempts=attempts, out_dir=task_dir, journal=journal
        )
        with results_path.open("a", encoding="utf-8") as fh:
            for r in results:
                fh.write(
                    json.dumps(
                        {
                            "task": r.task,
                            "attempt": r.attempt,
                            "solved": r.solved,
                            "iterations": r.iterations,
                            "model": cfg.model,
                            "duration_sec": r.duration_sec,
                            "error": r.error,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    for r in results:
        status = "SOLVED" if r.solved else "не решена"
        print(
            f"попытка {r.attempt}: {status} за {r.iterations} итер. ({r.duration_sec}s)"
            + (f" — {r.error}" if r.error and not r.solved else "")
        )
    solved_count = sum(1 for r in results if r.solved)
    print(
        f"итог {task.name} [{cfg.model}]: {solved_count}/{attempts}"
        f" за {round(time.monotonic() - started, 1)}s, артефакты: {task_dir}"
    )
    return results


if __name__ == "__main__":
    sys.exit(main())
