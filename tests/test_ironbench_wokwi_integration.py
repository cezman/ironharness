"""Integration test: all golden tasks in a real Wokwi cloud simulation.

It spends minutes of the free Wokwi quota (50 min/month), so it runs ONLY
deliberately: WOKWI_CLI_TOKEN (environment or .env) + IRONBENCH_REAL_WOKWI=1.
A bare token is not enough - a full `uv run pytest` must not burn the quota
(the 2026-09-07 lesson: the quota ran out mid-session from ordinary runs).
Neither exists on CI -> skip. A controlled run:
IRONBENCH_REAL_WOKWI=1 uv run pytest tests/test_ironbench_wokwi_integration.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ironbench.runner import resolve_token, run_task
from ironbench.tasks import Task, load_tasks

TASKS_DIR = Path(__file__).resolve().parents[1] / "src" / "ironbench" / "tasks"
TASKS = load_tasks(TASKS_DIR)

pytestmark = pytest.mark.skipif(
    not (resolve_token() and os.environ.get("IRONBENCH_REAL_WOKWI") == "1"),
    reason="real Wokwi burns the quota: WOKWI_CLI_TOKEN and IRONBENCH_REAL_WOKWI=1 are required",
)


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_golden_task_real_wokwi(task: Task, tmp_path):
    res = run_task(task, out_dir=tmp_path)
    assert res.passed, (
        f"exit={res.exit_code} error={res.error!r} "
        f"missed={res.missed} hit_fail={res.hit_fail}"
    )
