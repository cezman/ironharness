"""Загрузка описаний задач ironbench из YAML (task.yaml в каталоге задачи)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

TASK_FILE = "task.yaml"


@dataclasses.dataclass(frozen=True)
class Task:
    """Одна золотая задача: каталог Wokwi-проекта + критерии проверки serial-вывода.

    scenario=None → раннер сам генерирует REPL-paste сценарий из файла entry
    (MicroPython: код вставляется в REPL, см. runner.generate_paste_scenario).
    """

    name: str
    description: str
    directory: Path
    scenario: str | None
    entry: str
    timeout_sec: int
    expect: tuple[str, ...]
    fail: tuple[str, ...]


def load_task(task_dir: Path) -> Task:
    """Читает task.yaml из каталога задачи; ошибки формата — ValueError с путём."""
    task_file = task_dir / TASK_FILE
    if not task_file.is_file():
        raise ValueError(f"нет {TASK_FILE} в {task_dir}")
    raw = yaml.safe_load(task_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"{task_file}: ожидался YAML-словарь")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"{task_file}: обязательное поле name (строка) отсутствует")
    expect = raw.get("expect", [])
    fail = raw.get("fail", [])
    if not isinstance(expect, list) or not all(isinstance(p, str) for p in expect):
        raise ValueError(f"{task_file}: expect должен быть списком строк")
    if not isinstance(fail, list) or not all(isinstance(p, str) for p in fail):
        raise ValueError(f"{task_file}: fail должен быть списком строк")
    return Task(
        name=name,
        description=str(raw.get("description", "")),
        directory=task_dir,
        scenario=raw.get("scenario"),
        entry=str(raw.get("entry", "main.py")),
        timeout_sec=int(raw.get("timeout_sec", 30)),
        expect=tuple(expect),
        fail=tuple(fail),
    )


def load_tasks(tasks_dir: Path) -> list[Task]:
    """Все задачи каталога (подкаталоги с task.yaml), по алфавиту имён."""
    if not tasks_dir.is_dir():
        raise ValueError(f"каталог задач не найден: {tasks_dir}")
    tasks = [
        load_task(d)
        for d in sorted(tasks_dir.iterdir())
        if d.is_dir() and (d / TASK_FILE).is_file()
    ]
    return sorted(tasks, key=lambda t: t.name)
