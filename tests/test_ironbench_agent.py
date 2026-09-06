"""Тесты агентского цикла ironbench — фейковый LLM и фейковый раннер, без сети."""

from __future__ import annotations

from pathlib import Path

import pytest

import ironbench.agent as agent_module
from ironbench.agent import (
    SolveConfig,
    extract_code,
    resolve_llm_config,
    solve,
    solve_attempt,
    validate_endpoint,
)
from ironbench.runner import TaskResult
from ironbench.tasks import load_task

TASKS_DIR = Path(__file__).resolve().parents[1] / "src" / "ironbench" / "tasks"

GOOD_CODE = "print('hello')\n"
GOOD_RESPONSE = f"```python\n{GOOD_CODE}```"


def make_task():
    return load_task(TASKS_DIR / "blink")


def fake_runner(passed: bool, serial_tail: str = "Traceback ..."):
    """Раннер-стаб с контрактом run_task: TaskResult + serial-лог на диске."""

    def run(task, *, out_dir, journal=None):
        log_dir = Path(out_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"{task.name}.serial.log"
        log.write_text(serial_tail, encoding="utf-8")
        return TaskResult(
            task=task.name,
            passed=passed,
            exit_code=0 if passed else 42,
            duration_sec=0.1,
            serial_log=log,
        )

    return run


def test_extract_code():
    assert extract_code(GOOD_RESPONSE) == GOOD_CODE
    assert extract_code("пояснение без кода") is None
    two = "```python\na = 1\n```\nтекст\n```python\nb = 2\n```"
    assert extract_code(two) == "b = 2\n"
    assert extract_code("```micropython\nx=1\n```") == "x=1\n"


def test_resolve_llm_config_defaults_and_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # изолируемся и от репозиторного .env: тест проверяет только дефолты/окружение
    monkeypatch.setattr(agent_module, "find_env_file", lambda: None)
    cfg = resolve_llm_config()
    assert cfg.base_url == "http://localhost:1234/v1"
    assert cfg.model == "qwen3.5-9b"
    assert cfg.allow_local is True
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_ALLOW_LOCAL", "0")
    cfg = resolve_llm_config()
    assert cfg.model == "test-model"
    assert cfg.allow_local is False
    # явный аргумент сильнее окружения
    assert resolve_llm_config(model="explicit").model == "explicit"


def test_validate_endpoint_rules():
    validate_endpoint("http://localhost:1234/v1", allow_local=True)
    # публичный IP-литерал: без зависимости от внешнего DNS
    validate_endpoint("https://8.8.8.8/v1", allow_local=False)
    with pytest.raises(ValueError, match="http/https"):
        validate_endpoint("ftp://example.com", allow_local=True)
    with pytest.raises(ValueError, match="metadata"):
        validate_endpoint("http://metadata.google.internal/v1", allow_local=True)
    with pytest.raises(ValueError, match="приватн"):
        validate_endpoint("http://192.168.1.10:1234/v1", allow_local=False)


def test_chat_rejects_bad_scheme_before_network():
    cfg = SolveConfig(base_url="ftp://example.com", api_key="x", model="m")
    with pytest.raises(ValueError, match="http/https"):
        agent_module.chat(cfg, [])


def test_solve_attempt_solves_first_iteration(tmp_path):
    task = make_task()
    cfg = SolveConfig(base_url="http://x", api_key="k", model="m")
    seen: dict = {}

    def runner(work_task, *, out_dir, journal=None):
        seen["entry"] = work_task.entry
        seen["dir"] = work_task.directory
        return fake_runner(True)(work_task, out_dir=out_dir, journal=journal)

    res = solve_attempt(
        task, cfg, out_dir=tmp_path, llm=lambda cfg, msgs: GOOD_RESPONSE, runner=runner
    )
    assert res.solved and res.iterations == 1
    assert seen["entry"] == "main.py"
    assert (seen["dir"] / "main.py").read_text(encoding="utf-8") == GOOD_CODE
    # solution.py не должен утечь в рабочий каталог агента
    assert not (seen["dir"] / "solution.py").exists()
    assert (seen["dir"] / "wokwi.toml").exists()  # остальная обвязка задачи копируется


def test_solve_attempt_hits_iteration_limit(tmp_path):
    task = make_task()
    cfg = SolveConfig(base_url="http://x", api_key="k", model="m", max_iterations=3)
    res = solve_attempt(
        task,
        cfg,
        out_dir=tmp_path,
        llm=lambda cfg, msgs: GOOD_RESPONSE,
        runner=fake_runner(False),
    )
    assert not res.solved
    assert res.iterations == 3
    assert "лимит итераций" in (res.error or "")


def test_solve_attempt_asks_again_without_code_block(tmp_path):
    task = make_task()
    cfg = SolveConfig(base_url="http://x", api_key="k", model="m")
    responses = iter(["ответ без кода", GOOD_RESPONSE])

    def llm(cfg, msgs):
        # после первого «без кода» должен прийти feedback-ход от пользователя
        assert len(msgs) >= 2
        return next(responses)

    res = solve_attempt(task, cfg, out_dir=tmp_path, llm=llm, runner=fake_runner(True))
    assert res.solved and res.iterations == 2


def test_solve_attempt_llm_error_is_clean_fail(tmp_path):
    task = make_task()
    cfg = SolveConfig(base_url="http://x", api_key="k", model="m")

    def boom(cfg, msgs):
        raise ConnectionError("server down")

    res = solve_attempt(task, cfg, out_dir=tmp_path, llm=boom, runner=fake_runner(True))
    assert not res.solved
    assert "ошибка LLM" in (res.error or "")


def test_solve_writes_journal_and_counts_attempts(tmp_path):
    task = make_task()
    cfg = SolveConfig(base_url="http://x", api_key="k", model="test-model")
    jpath = tmp_path / "journal.jsonl"

    class J:
        def __call__(self, kind, data):
            events.append((kind, data))

    events: list = []
    results = solve(
        task,
        cfg,
        attempts=2,
        out_dir=tmp_path / "camp",
        llm=lambda cfg, msgs: GOOD_RESPONSE,
        runner=fake_runner(True),
        journal=J(),
    )
    assert len(results) == 2
    kinds = [k for k, _ in events]
    assert "attempt_result" in kinds
    attempt_events = [d for k, d in events if k == "attempt_result"]
    assert attempt_events[0]["model"] == "test-model"
    assert jpath.exists() is False  # Journal-стаб не пишет файл — пишет реальный JsonlJournal
