"""Интеграционный тест: все золотые задачи в реальной облачной симуляции Wokwi.

Тратит минуты бесплатной квоты Wokwi (50 мин/мес), поэтому без WOKWI_CLI_TOKEN
(в окружении или .env) — скипается. На CI токена нет → скип; добавите секрет — заработает.
Запускать выборочно: `uv run pytest tests/test_ironbench_wokwi_integration.py -k debounce`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ironbench.runner import resolve_token, run_task
from ironbench.tasks import Task, load_tasks

TASKS_DIR = Path(__file__).resolve().parents[1] / "src" / "ironbench" / "tasks"
TASKS = load_tasks(TASKS_DIR)

pytestmark = pytest.mark.skipif(
    not resolve_token(), reason="нет WOKWI_CLI_TOKEN (окружение или .env)"
)


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_golden_task_real_wokwi(task: Task, tmp_path):
    res = run_task(task, out_dir=tmp_path)
    assert res.passed, (
        f"exit={res.exit_code} error={res.error!r} "
        f"missed={res.missed} hit_fail={res.hit_fail}"
    )
