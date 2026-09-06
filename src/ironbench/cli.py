"""CLI ironbench: `ironbench run --task blink` / `--all`, `ironbench list`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from io_core.journal import JsonlJournal
from ironbench import __version__
from ironbench.runner import run_task
from ironbench.tasks import load_tasks

DEFAULT_TASKS_DIR = Path(__file__).resolve().parent / "tasks"


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

    run = sub.add_parser("run", parents=[common], help="запустить задачи")
    run.add_argument("--task", help="имя задачи (например, blink)")
    run.add_argument("--all", action="store_true", help="запустить все задачи")

    sub.add_parser("list", parents=[common], help="показать доступные задачи")

    args = parser.parse_args(argv)
    tasks = load_tasks(args.tasks_dir)

    if args.command == "list":
        for t in tasks:
            print(f"{t.name}: {t.description}")
        return 0

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
            all_passed = all_passed and res.passed
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
