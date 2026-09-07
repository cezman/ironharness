"""Загрузка описаний задач ironbench из YAML (task.yaml в каталоге задачи)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

from io_core.faults import Fault
from ironbench.plant import PLANT_MODELS

TASK_FILE = "task.yaml"

# Мишени запуска задачи; real (этап 3) — в плане. unix = MicroPython unix-port
# в WSL2: бесплатные локальные прогоны чисто-serial задач (см. runner._run_unix);
# plant = закрытая петля «объект + регулятор» в Python (см. runner._run_plant)
TASK_TARGETS = ("wokwi", "renode", "unix", "plant", "real")

# Типы шагов сценария wokwi, разрешённые в stimulus; расширять вместе с wokwi-cli
STIMULUS_STEP_KEYS = frozenset({"write-serial", "wait-serial", "delay", "set-control"})

# Ключи секции renode в task.yaml: платформа (.repl из поставки Renode), прошивка
# (.elf из каталога задачи или tasks/_firmware), имя UART-периферии для терминала
RENODE_KEYS = frozenset({"platform", "firmware", "uart"})

# Секция noise: шумная линия поверх стимула мишени unix. seed — детерминизм,
# faults — те же сценарии, что у io_core.FaultyTransport (словари Fault)
NOISE_KEYS = frozenset({"seed", "faults"})

# Секция plant (мишень plant): физика объекта 1-го порядка + требования к
# переходной характеристике. Смысл ключей — в ironbench/plant.py
PLANT_KEYS = frozenset(
    {
        "model",
        "K",
        "T",
        "ambient",
        "y0",
        "dt",
        "duration",
        "u_min",
        "u_max",
        "noise_std",
        "seed",
        "setpoint",
        "requirements",
        "disturbances",
    }
)
PLANT_REQUIREMENT_KEYS = frozenset({"steady_error", "overshoot", "settle_time"})
PLANT_DISTURBANCE_KEYS = frozenset({"at", "ambient"})

# Таксономия бенчмарка: тег класса — ядро навыка задачи, level — ступень
# сложности 1..5. Отчёт показывает профиль модели по классам, а не одно число
CLASS_TAGS = ("io", "data", "protocol", "fsm", "control", "resilience")


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
    stimulus: tuple[dict, ...] = ()
    target: str = "wokwi"
    renode: dict = dataclasses.field(default_factory=dict)
    noise: dict = dataclasses.field(default_factory=dict)
    plant: dict = dataclasses.field(default_factory=dict)
    tags: tuple[str, ...] = ()
    level: int | None = None


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
    scenario = raw.get("scenario")
    if scenario is not None and not isinstance(scenario, str):
        raise ValueError(f"{task_file}: scenario должен быть строкой (путь к YAML)")
    try:
        timeout_sec = int(raw.get("timeout_sec", 30))
    except (TypeError, ValueError):
        raise ValueError(f"{task_file}: timeout_sec должен быть целым числом") from None
    stimulus = raw.get("stimulus", [])
    if not isinstance(stimulus, list) or not all(isinstance(s, dict) for s in stimulus):
        raise ValueError(f"{task_file}: stimulus должен быть списком шагов (словарей)")
    for step in stimulus:
        unknown = set(step) - STIMULUS_STEP_KEYS
        if unknown:
            raise ValueError(
                f"{task_file}: неизвестный шаг stimulus {sorted(unknown)} "
                f"(разрешены: {sorted(STIMULUS_STEP_KEYS)})"
            )
    target = str(raw.get("target", "wokwi"))
    if target not in TASK_TARGETS:
        raise ValueError(f"{task_file}: неизвестная мишень {target!r} (разрешены: {TASK_TARGETS})")
    renode = raw.get("renode", {})
    if not isinstance(renode, dict):
        raise TypeError(f"{task_file}: renode должен быть словарём (platform/firmware/uart)")
    unknown_renode = set(renode) - RENODE_KEYS
    if unknown_renode:
        raise ValueError(
            f"{task_file}: неизвестные ключи renode {sorted(unknown_renode)} "
            f"(разрешены: {sorted(RENODE_KEYS)})"
        )
    if target == "renode":
        missing = {"platform", "firmware"} - set(renode)
        if missing:
            raise ValueError(
                f"{task_file}: для мишени renode в секции renode нужны platform и firmware "
                f"(нет: {sorted(missing)})"
            )
        for key in ("platform", "firmware", "uart"):
            if key in renode and (not isinstance(renode[key], str) or not renode[key]):
                raise ValueError(f"{task_file}: renode.{key} должен быть непустой строкой")
    noise = raw.get("noise", {})
    if not isinstance(noise, dict):
        raise TypeError(f"{task_file}: noise должен быть словарём (seed/faults)")
    unknown_noise = set(noise) - NOISE_KEYS
    if unknown_noise:
        raise ValueError(
            f"{task_file}: неизвестные ключи noise {sorted(unknown_noise)} "
            f"(разрешены: {sorted(NOISE_KEYS)})"
        )
    if "seed" in noise and (
        isinstance(noise["seed"], bool) or not isinstance(noise["seed"], int)
    ):
        raise ValueError(f"{task_file}: noise.seed должен быть целым числом")
    faults = noise.get("faults", [])
    if not isinstance(faults, list) or not all(isinstance(f, dict) for f in faults):
        raise ValueError(f"{task_file}: noise.faults должен быть списком словарей")
    for f in faults:
        # disconnect ронял бы прогон (ConnectionLost не ловится в _run_unix),
        # остальные действия шумной линии честно поддержаны
        if f.get("action") not in {"drop", "corrupt", "delay"}:
            raise ValueError(
                f"{task_file}: noise.faults: действие {f.get('action')!r} не поддерживается "
                "(разрешены: drop, corrupt, delay)"
            )
    try:
        [Fault(**f) for f in faults]  # валидация сценариев сбоев на этапе загрузки
    except (TypeError, ValueError) as e:
        raise ValueError(f"{task_file}: noise.faults: {e}") from None
    if noise and target != "unix":
        raise ValueError(f"{task_file}: noise поддерживается только мишенью unix")
    plant_section = raw.get("plant", {})
    if plant_section and target != "plant":
        raise ValueError(f"{task_file}: секция plant поддерживается только мишенью plant")
    if target == "plant":
        if not isinstance(plant_section, dict):
            raise TypeError(f"{task_file}: секция plant должна быть словарём")
        if not plant_section:
            raise ValueError(f"{task_file}: для мишени plant нужна непустая секция plant")
        unknown_plant = set(plant_section) - PLANT_KEYS
        if unknown_plant:
            raise ValueError(
                f"{task_file}: неизвестные ключи plant {sorted(unknown_plant)} "
                f"(разрешены: {sorted(PLANT_KEYS)})"
            )
        model = str(plant_section.get("model", "heater"))
        if model not in PLANT_MODELS:
            raise ValueError(
                f"{task_file}: plant.model {model!r} не поддержан (разрешены: {PLANT_MODELS})"
            )
        for key in ("K", "T", "duration", "setpoint"):
            if key not in plant_section:
                raise ValueError(f"{task_file}: в секции plant обязателен ключ {key}")
        for key, value in plant_section.items():
            if key in ("model", "requirements", "disturbances"):
                continue
            if key == "seed":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{task_file}: plant.seed должен быть целым числом")
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                # контракт загрузчика — любые ошибки формата как ValueError
                raise ValueError(f"{task_file}: plant.{key} должен быть числом")  # noqa: TRY004
        if plant_section["K"] <= 0:
            raise ValueError(f"{task_file}: plant.K должен быть > 0")
        if plant_section["T"] <= 0:
            raise ValueError(f"{task_file}: plant.T должен быть > 0")
        dt = float(plant_section.get("dt", 0.5))
        if dt <= 0 or dt > float(plant_section["duration"]):
            raise ValueError(f"{task_file}: plant.dt должен быть > 0 и не больше duration")
        if float(plant_section.get("u_min", 0.0)) >= float(plant_section.get("u_max", 1.0)):
            raise ValueError(f"{task_file}: plant.u_min должен быть меньше u_max")
        if float(plant_section.get("noise_std", 0.0)) < 0:
            raise ValueError(f"{task_file}: plant.noise_std должен быть >= 0")
        requirements = plant_section.get("requirements")
        if not isinstance(requirements, dict) or not requirements:
            raise ValueError(
                f"{task_file}: plant.requirements — непустой словарь допусков "
                f"{sorted(PLANT_REQUIREMENT_KEYS)}"
            )
        unknown_req = set(requirements) - PLANT_REQUIREMENT_KEYS
        if unknown_req:
            raise ValueError(
                f"{task_file}: неизвестные ключи plant.requirements {sorted(unknown_req)} "
                f"(разрешены: {sorted(PLANT_REQUIREMENT_KEYS)})"
            )
        for key, value in requirements.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{task_file}: plant.requirements.{key} должен быть числом > 0")
        disturbances = plant_section.get("disturbances", [])
        if not isinstance(disturbances, list):
            raise TypeError(f"{task_file}: plant.disturbances должен быть списком словарей")
        if not all(isinstance(d, dict) for d in disturbances):
            raise TypeError(f"{task_file}: plant.disturbances должен быть списком словарей")
        for d in disturbances:
            unknown_dist = set(d) - PLANT_DISTURBANCE_KEYS
            if unknown_dist:
                raise ValueError(
                    f"{task_file}: неизвестные ключи события disturbances {sorted(unknown_dist)} "
                    f"(разрешены: {sorted(PLANT_DISTURBANCE_KEYS)})"
                )
            if "at" not in d or "ambient" not in d:
                raise ValueError(f"{task_file}: в событии disturbances нужны at и ambient")
            if isinstance(d["at"], bool) or not isinstance(d["at"], (int, float)) or d["at"] < 0:
                raise ValueError(f"{task_file}: disturbances.at должен быть числом >= 0")
            if isinstance(d["ambient"], bool) or not isinstance(d["ambient"], (int, float)):
                raise ValueError(f"{task_file}: disturbances.ambient должен быть числом")  # noqa: TRY004
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError(f"{task_file}: tags должен быть списком строк")
    if len(set(tags)) != len(tags):
        raise ValueError(f"{task_file}: tags содержит дубликаты")
    unknown_tags = set(tags) - set(CLASS_TAGS)
    if unknown_tags:
        raise ValueError(
            f"{task_file}: неизвестные теги {sorted(unknown_tags)} (разрешены: {CLASS_TAGS})"
        )
    level = raw.get("level")
    if level is not None and (
        isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5
    ):
        raise ValueError(f"{task_file}: level должен быть целым числом 1..5")
    return Task(
        name=name,
        description=str(raw.get("description", "")),
        directory=task_dir,
        scenario=scenario,
        entry=str(raw.get("entry", "main.py")),
        timeout_sec=timeout_sec,
        expect=tuple(expect),
        fail=tuple(fail),
        stimulus=tuple(stimulus),
        target=target,
        renode=dict(renode),
        noise=dict(noise),
        plant=dict(plant_section),
        tags=tuple(tags),
        level=level,
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
