"""Интеграционный тест: реальный запуск blink в облачной симуляции Wokwi.

Тратит минуты бесплатной квоты Wokwi (50 мин/мес), поэтому без WOKWI_CLI_TOKEN
(в окружении или .env) — скипается. На CI токена нет → скип; добавите секрет — заработает.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ironbench.runner import resolve_token, run_task
from ironbench.tasks import load_task

BLINK_DIR = Path(__file__).resolve().parents[1] / "src" / "ironbench" / "tasks" / "blink"

pytestmark = pytest.mark.skipif(
    not resolve_token(), reason="нет WOKWI_CLI_TOKEN (окружение или .env)"
)


def test_blink_real_wokwi(tmp_path):
    task = load_task(BLINK_DIR)
    res = run_task(task, out_dir=tmp_path)
    assert res.passed, (
        f"exit={res.exit_code} error={res.error!r} "
        f"missed={res.missed} hit_fail={res.hit_fail}"
    )
