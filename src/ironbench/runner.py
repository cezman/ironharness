"""Раннер задач ironbench: мишени (план 3.2) — wokwi (реализована), renode (2.6) и
real (этап 3) пока дают честный FAIL; serial-лог → оценка по паттернам.

Оценка принадлежит раннеру (не сценарию Wokwi): после каждого запуска serial-лог
перепроверяется на expect/fail-паттерны из task.yaml.

MicroPython в wokwi-cli не автозапускает main.py (голая прошивка + REPL), поэтому
если у задачи нет статического scenario, раннер генерирует REPL-paste сценарий:
ждёт приглашения '>>>', вставляет код entry-файла в raw-paste режиме (Ctrl+E/Ctrl+D)
и ждёт ожидаемые строки. У золотых задач entry — solution.py (эталон); main.py —
файл, который в бенчмарке пишет агент.

Выход wokwi-cli: 0 — сценарий завершился, 42 — сработал --timeout. Для прошивки с
бесконечным циклом 42 — норма, поэтому успешными считаются оба кода при полном
совпадении паттернов. Прогоны журналируются через io_core.JsonlJournal.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import yaml

from ironbench.tasks import Task

# Коды возврата wokwi-cli: сработавший --timeout (42) для бесконечных прошивок — норма
WOKWI_TIMEOUT_EXIT = 42
OK_EXIT_CODES = (0, WOKWI_TIMEOUT_EXIT)

# Запас настенного времени поверх симуляционного лимита (старт симуляции в облаке).
# Модульная константа — тесты подменяют её, чтобы не ждать по-настоящему.
WALL_GRACE_SEC = 15

# Приглашение REPL MicroPython, по которому понимаем, что устройство готово к вставке
REPL_PROMPT = ">>>"

# Общий каталог закреплённых прошивок: в каталоге задачи бин не дублируем,
# раннер докладывает его в стейдж по ссылкам elf/firmware из wokwi.toml
FIRMWARE_DIR = Path(__file__).resolve().parent / "tasks" / "_firmware"


def load_env_file(path: Path) -> dict[str, str]:
    """Разбирает .env: KEY=VALUE / export KEY=VALUE / $env:KEY='VALUE' (стиль владельца)."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ps = re.match(r"^\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        exported = re.match(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        plain = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        m = ps or exported or plain
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def find_env_file() -> Path | None:
    """Первый существующий .env: в cwd или в корне репозитория (два уровня выше src/)."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    return next((p for p in candidates if p.is_file()), None)


def resolve_token(explicit: str | None = None) -> str | None:
    """Токен Wokwi: явный аргумент > переменная окружения WOKWI_CLI_TOKEN > .env."""
    if explicit:
        return explicit
    if os.environ.get("WOKWI_CLI_TOKEN"):
        return os.environ["WOKWI_CLI_TOKEN"]
    env_file = find_env_file()
    if env_file:
        return load_env_file(env_file).get("WOKWI_CLI_TOKEN")
    return None


def default_cli() -> str:
    """Путь к wokwi-cli: в PATH или стандартное место установщика (~/.wokwi/bin)."""
    found = shutil.which("wokwi-cli")
    if found:
        return found
    exe = "wokwi-cli.exe" if sys.platform == "win32" else "wokwi-cli"
    candidate = Path.home() / ".wokwi" / "bin" / exe
    return str(candidate) if candidate.is_file() else "wokwi-cli"


def _plain_text(pattern: str) -> str | None:
    """Паттерн без regex-метасимволов годится для wait-serial (досрочное завершение)."""
    return pattern if not re.search(r"[\\^$.|?*+()\[\]{}]", pattern) else None


def generate_paste_scenario(task: Task) -> str:
    """YAML сценария: вставить код entry-файла в REPL, выполнить stimulus, ждать expect."""
    code = (task.directory / task.entry).read_text(encoding="utf-8")
    steps: list[dict[str, object]] = [
        {"wait-serial": REPL_PROMPT},
        {"write-serial": "\x05"},  # Ctrl+E: raw-paste режим
        {"delay": "200ms"},
        {"write-serial": code},
        {"write-serial": "\x04"},  # Ctrl+D: выполнить
        *task.stimulus,  # взаимодействие с прошивкой (ввод serial, кнопки, датчики)
    ]
    # wait-serial по литеральным expect-паттернам: сценарий завершит симуляцию
    # досрочно, когда всё ожидаемое уже напечатано (экономия квоты Wokwi)
    for pattern in task.expect:
        plain = _plain_text(pattern)
        if plain:
            steps.append({"wait-serial": plain})
    doc = {
        "name": f"{task.name}-paste",
        "version": 1,
        "author": "ironbench",
        "steps": steps,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


@dataclasses.dataclass(frozen=True)
class TaskResult:
    """Итог прогона одной задачи."""

    task: str
    passed: bool
    exit_code: int | None
    duration_sec: float
    serial_log: Path | None
    missed: tuple[str, ...] = ()
    hit_fail: tuple[str, ...] = ()
    error: str | None = None


def _check_patterns(
    serial_text: str, expect: tuple[str, ...], fail: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    missed = tuple(p for p in expect if not re.search(p, serial_text))
    hit_fail = tuple(p for p in fail if re.search(p, serial_text))
    return missed, hit_fail


def _stage_task(task: Task, out_dir: Path) -> tuple[Path, str]:
    """Копирует каталог задачи в чистый стейдж; возвращает (путь, имя сценария)."""
    stage = out_dir / task.name
    shutil.rmtree(stage, ignore_errors=True)  # без этого старые файлы переживают прогон
    stage.mkdir(parents=True, exist_ok=True)
    for item in task.directory.iterdir():
        if item.is_file():
            shutil.copy2(item, stage / item.name)
    _stage_firmware(task, stage)
    if task.scenario:
        return stage, task.scenario
    scenario_file = stage / "generated.scenario.yaml"
    scenario_file.write_text(generate_paste_scenario(task), encoding="utf-8")
    return stage, scenario_file.name


def _stage_firmware(task: Task, stage: Path) -> None:
    """Докладывает elf/firmware из общего FIRMWARE_DIR, если в задаче их нет."""
    wokwi_toml = stage / "wokwi.toml"
    if not wokwi_toml.is_file():
        return
    config = tomllib.loads(wokwi_toml.read_text(encoding="utf-8")).get("wokwi", {})
    for key in ("elf", "firmware"):
        name = config.get(key)
        if isinstance(name, str) and not (stage / name).is_file():
            shared = FIRMWARE_DIR / name
            if shared.is_file():
                shutil.copy2(shared, stage / name)
            else:
                raise ValueError(
                    f"прошивка {name!r} не найдена ни в задаче, ни в tasks/_firmware/"
                )


def _journal_result(journal, result: TaskResult) -> None:
    if journal:
        journal(
            "task_result",
            {
                "task": result.task,
                "passed": result.passed,
                "exit_code": result.exit_code,
                "duration_sec": result.duration_sec,
                "missed": list(result.missed),
                "hit_fail": list(result.hit_fail),
                "error": result.error,
            },
        )


def run_task(
    task: Task,
    *,
    out_dir: Path,
    cli_path: str | None = None,
    token: str | None = None,
    journal=None,
) -> TaskResult:
    """Диспетчер мишеней (план 3.2): wokwi реализована, renode/real — честный FAIL.

    cli_path/token — точки инъекции для тестов (фейковый CLI вместо реального).
    journal — io_core.JsonlJournal: пишем task_start/task_result.
    """
    if task.target == "wokwi":
        return _run_wokwi(task, out_dir=out_dir, cli_path=cli_path, token=token, journal=journal)
    stage_note = {
        "renode": "этап 2.6: нужен Renode-бэкенд (WSL2)",
        "real": "этап 3: нужны живая плата (usbipd) и бэкенд real",
    }
    result = TaskResult(
        task=task.name,
        passed=False,
        exit_code=None,
        duration_sec=0.0,
        serial_log=None,
        missed=tuple(task.expect),
        error=(
            f"мишень {task.target!r} не реализована ({stage_note.get(task.target, 'вне плана')}); "
            "проверки не выполнялись"
        ),
    )
    _journal_result(journal, result)
    return result


def _run_wokwi(
    task: Task,
    *,
    out_dir: Path,
    cli_path: str | None = None,
    token: str | None = None,
    journal=None,
) -> TaskResult:
    """Запускает задачу в Wokwi и возвращает результат (pass/fail + причина)."""
    cli = cli_path or default_cli()
    cli_cmd = [cli] if isinstance(cli, str) else list(cli)  # тесты передают список-команду
    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{task.name}.serial.log"
    wall_timeout = task.timeout_sec * 2 + WALL_GRACE_SEC

    token = resolve_token(token)
    env = {**os.environ}
    if token:
        env["WOKWI_CLI_TOKEN"] = token

    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    stage = scenario_name = None
    try:
        stage, scenario_name = _stage_task(task, out_dir)
    except (OSError, ValueError) as e:
        # отсутствующий entry/прошивка, битый wokwi.toml — аккуратный FAIL вместо краха
        error = f"не удалось подготовить задачу: {e}"
    if stage is not None:
        cmd = [
            *cli_cmd,
            str(stage),
            "--scenario",
            scenario_name,
            "--timeout",
            str(task.timeout_sec * 1000),
            "--serial-log-file",
            str(serial_log),
            "--timeout-exit-code",
            str(WOKWI_TIMEOUT_EXIT),
            "-q",
        ]
        if journal:
            journal("task_start", {"task": task.name})
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=wall_timeout,
                check=False,
            )
            exit_code = proc.returncode
            if proc.returncode not in OK_EXIT_CODES:
                error = (proc.stderr or proc.stdout or "").strip()[-500:] or None
        except subprocess.TimeoutExpired:
            error = f"wall-clock таймаут раннера ({wall_timeout} c)"
        except FileNotFoundError:
            error = f"wokwi-cli не найден: {cli}"

    duration = round(time.monotonic() - start, 2)
    serial_text = (
        serial_log.read_text(encoding="utf-8", errors="replace") if serial_log.is_file() else ""
    )
    missed, hit_fail = _check_patterns(serial_text, task.expect, task.fail)
    passed = exit_code in OK_EXIT_CODES and not missed and not hit_fail and error is None
    result = TaskResult(
        task=task.name,
        passed=passed,
        exit_code=exit_code,
        duration_sec=duration,
        serial_log=serial_log if serial_text else None,
        missed=missed,
        hit_fail=hit_fail,
        error=error,
    )
    _journal_result(journal, result)
    return result
