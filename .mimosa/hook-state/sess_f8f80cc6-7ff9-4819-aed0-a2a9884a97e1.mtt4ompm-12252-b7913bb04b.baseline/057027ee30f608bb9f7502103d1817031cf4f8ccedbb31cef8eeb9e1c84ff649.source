"""Интеграционный тест: все золотые задачи в реальной облачной симуляции Wokwi.

Тратит минуты бесплатной квоты Wokwi (50 мин/мес), поэтому запускается ТОЛЬКО
осознанно: WOKWI_CLI_TOKEN (окружение или .env) + IRONBENCH_REAL_WOKWI=1.
Голый токен недостаточен — полный `uv run pytest` не должен жечь квоту
(урок 2026-09-07: квота кончилась посреди сессии от обычных прогонов).
На CI обоих нет → скип. Контрольный прогон:
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
    reason="реальный Wokwi жжёт квоту: нужны WOKWI_CLI_TOKEN и IRONBENCH_REAL_WOKWI=1",
)


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_golden_task_real_wokwi(task: Task, tmp_path):
    res = run_task(task, out_dir=tmp_path)
    assert res.passed, (
        f"exit={res.exit_code} error={res.error!r} "
        f"missed={res.missed} hit_fail={res.hit_fail}"
    )
