"""Агентский цикл ironbench (2.4): LLM пишет main.py для задачи, раннер проверяет.

Минимальный loop без function calling: агент получает постановку задачи и последний
serial-вывод, отвечает одним блоком ```python с полным кодом main.py; код исполняется
раннером (REPL-paste в Wokwi), результат — обратная связь. Так цикл работает с любой
OpenAI-совместимой моделью, включая локальные LM Studio/ollama без поддержки тулзов.

Конфиг — переменные окружения (или .env): LLM_BASE_URL (по умолчанию локальный
LM Studio), LLM_API_KEY (для локальных сойдёт фиктивный), LLM_MODEL.

Безопасность исходящих запросов: схема только http/https, редиректы запрещены,
link-local и облачный metadata-хосты заблокированы всегда, приватные/loopback
адреса разрешены только флагом LLM_ALLOW_LOCAL=1 (по умолчанию включён — проект
заточен под локальный LLM; выключение оставляет только публичные эндпоинты).
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ironbench.runner import find_env_file, is_infra_error, load_env_file, run_task
from ironbench.tasks import Task

SYSTEM_PROMPT = (
    "Ты — embedded-инженер. Пишешь прошивку MicroPython для ESP32 в виде цельного "
    "скрипта верхнего уровня. Отвечай только одним блоком ```python с полным кодом "
    "main.py, без пояснений."
)

CODE_FENCE = re.compile(r"```(?:python|micropython)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# Название файла, который пишет агент (в золотых задачах это место занято solution.py)
AGENT_FILE = "main.py"

# Сколько строк serial-вывода показываем агенту как обратную связь
SERIAL_FEEDBACK_LINES = 40

# Всегда запрещённые цели (облачный metadata) — даже при LLM_ALLOW_LOCAL=1
ALWAYS_BLOCKED_IPS = frozenset({ipaddress.ip_address("169.254.169.254")})


@dataclasses.dataclass(frozen=True)
class SolveConfig:
    base_url: str
    api_key: str
    model: str
    max_iterations: int = 5
    temperature: float = 0.7
    timeout_sec: int = 600
    allow_local: bool = True
    # потолок генерации: без него рассуждающие модели на сложных задачах
    # зависают в бесконечном "думании" и занимают очередь сервера
    max_tokens: int = 8192


def _env_map() -> dict[str, str]:
    env_file = find_env_file()
    return load_env_file(env_file) if env_file else {}


def _pick(explicit: str | None, names: tuple[str, ...], default: str) -> str:
    if explicit:
        return explicit
    env = _env_map()
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
        if env.get(name):
            return env[name]
    return default


def resolve_llm_config(
    base_url: str | None = None, api_key: str | None = None, model: str | None = None
) -> SolveConfig:
    """Конфиг LLM: явные аргументы > окружение > .env > значения по умолчанию."""
    return SolveConfig(
        base_url=_pick(base_url, ("LLM_BASE_URL",), "http://localhost:1234/v1"),
        api_key=_pick(api_key, ("LLM_API_KEY",), "lm-studio"),
        model=_pick(model, ("LLM_MODEL",), "qwen3.5-9b"),
        max_iterations=int(_pick(None, ("LLM_MAX_ITERATIONS",), "5") or 5),
        # локальные 9B с длинным контекстом думают по несколько минут на вызов
        timeout_sec=int(_pick(None, ("LLM_TIMEOUT",), "600") or 600),
        allow_local=_pick(None, ("LLM_ALLOW_LOCAL",), "1").strip().lower() not in ("0", "false", "no"),
        max_tokens=int(_pick(None, ("LLM_MAX_TOKENS",), "8192") or 8192),
    )


def validate_endpoint(base_url: str, *, allow_local: bool) -> None:
    """Границы SSRF: схема, резолв хоста, запрет metadata/link-local и (опц.) приватных сетей."""
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"LLM_BASE_URL должен быть http/https: {base_url}")
    host = (urllib.parse.urlsplit(base_url).hostname or "").rstrip(".")
    if not host:
        raise ValueError(f"LLM_BASE_URL без хоста: {base_url}")
    if host.lower() in ("metadata.google.internal", "metadata"):
        raise ValueError(f"заблокированный metadata-хост: {host}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"хост LLM_BASE_URL не резолвится: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # is_reserved не трогаем: у IPv6 он ложится и на ::1
        if ip in ALWAYS_BLOCKED_IPS or ip.is_link_local or ip.is_multicast:
            raise ValueError(f"заблокированный адрес {host} -> {ip}")
        if not allow_local and (ip.is_private or ip.is_loopback or ip.is_unspecified):
            raise ValueError(
                f"приватный/loopback адрес {host} -> {ip} запрещён при LLM_ALLOW_LOCAL=0"
            )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редиректы запрещены: URL проверяется до запроса, переадресация обойдёт границу."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def chat(cfg: SolveConfig, messages: list[dict]) -> str:
    """Один вызов /chat/completions без SDK (хватает urllib для локального сервера)."""
    validate_endpoint(cfg.base_url, allow_local=cfg.allow_local)
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    with _OPENER.open(req, timeout=cfg.timeout_sec) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"] or ""


def extract_code(response: str) -> str | None:
    """Последний блок ```python из ответа; без блоков — None."""
    blocks = CODE_FENCE.findall(response)
    if not blocks:
        return None
    code = blocks[-1].strip()
    return code + "\n" if code else None


def _first_prompt(task: Task) -> str:
    if task.target == "plant":
        return (
            f"Задача: {task.description}\n\n"
            f"Напиши полный код файла {AGENT_FILE} — контроллер замкнутой системы "
            "в виде функции control(t, y, setpoint). "
            "Ответ — только один блок ```python с полным кодом."
        )
    return (
        f"Задача: {task.description}\n\n"
        f"Напиши полный код файла {AGENT_FILE} для MicroPython ESP32. "
        "Печать в serial — обычный print(). "
        "Ответ — только один блок ```python с полным кодом."
    )


def _serial_feedback(serial_log: Path | None) -> str:
    if serial_log is None or not serial_log.is_file():
        return "(serial-вывод пуст — прошивка не запустилась)"
    lines = serial_log.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-SERIAL_FEEDBACK_LINES:])


def _work_task(task: Task, work_dir: Path) -> Task:
    """Копия задачи в рабочий каталог: entry — main.py агента, без solution.py."""
    work_dir.mkdir(parents=True, exist_ok=True)
    for item in task.directory.iterdir():
        if item.is_file() and item.name not in ("solution.py", "task.yaml"):
            (work_dir / item.name).write_bytes(item.read_bytes())
    return dataclasses.replace(task, directory=work_dir, entry=AGENT_FILE)


@dataclasses.dataclass(frozen=True)
class AttemptResult:
    task: str
    attempt: int
    solved: bool
    iterations: int
    duration_sec: float
    work_dir: Path
    error: str | None = None


def solve_attempt(
    task: Task,
    cfg: SolveConfig,
    *,
    attempt: int = 1,
    out_dir: Path,
    llm=chat,
    runner=run_task,
    journal=None,
) -> AttemptResult:
    """Одна попытка решить задачу: цикл «ответ LLM → main.py → прогон → фидбек».

    llm/runner — точки инъекции для офлайн-тестов (фейковый LLM и раннер).
    """
    attempt_dir = out_dir / f"attempt-{attempt}"
    work_dir = attempt_dir / "work"
    work_task = _work_task(task, work_dir)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _first_prompt(task)},
    ]
    start = time.monotonic()
    solved = False
    error: str | None = None
    iterations = 0
    while iterations < cfg.max_iterations:
        iterations += 1
        try:
            response = llm(cfg, messages)
        except (OSError, ValueError, LookupError, TypeError) as e:
            # сеть/HTTP/битый ответ сервера LLM (в т.ч. пустой "choices") —
            # ошибка попытки, а не падение раннера
            error = f"ошибка LLM: {e}"
            break
        code = extract_code(response)
        if code is None:
            messages.append({"role": "assistant", "content": response})
            messages.append(
                {
                    "role": "user",
                    "content": "В ответе нет блока ```python. Повтори ответ: только один "
                    "блок ```python с полным кодом main.py.",
                }
            )
            continue
        (work_dir / AGENT_FILE).write_text(code, encoding="utf-8")
        if journal:
            journal("iteration", {"task": task.name, "attempt": attempt, "n": iterations})
        result = runner(work_task, out_dir=attempt_dir, journal=journal)
        # артефакты итерации: код и serial-вывод сохраняются до перезаписи следующим ходом
        (attempt_dir / f"iter-{iterations}.main.py").write_text(code, encoding="utf-8")
        if result.serial_log and result.serial_log.is_file():
            (attempt_dir / f"iter-{iterations}.serial.log").write_bytes(
                result.serial_log.read_bytes()
            )
        if result.passed:
            solved = True
            break
        if is_infra_error(result.error):
            # среда сломана (нет Renode/прошивки/CLI) — LLM это не починит,
            # дальнейшие итерации только жгут токены
            error = f"среда не готова, попытка остановлена: {result.error}"
            break
        messages.append({"role": "assistant", "content": response})
        messages.append(
            {
                "role": "user",
                "content": "Проверка не пройдена. Вывод прогона:\n\n"
                f"{_serial_feedback(result.serial_log)}\n\n"
                "Исправь код и пришли снова только один блок ```python с полным main.py.",
            }
        )
    if error is None and not solved:
        error = f"лимит итераций ({cfg.max_iterations}) исчерпан"

    duration = round(time.monotonic() - start, 2)
    return AttemptResult(
        task=task.name,
        attempt=attempt,
        solved=solved,
        iterations=iterations,
        duration_sec=duration,
        work_dir=work_dir,
        error=error,
    )


def solve(
    task: Task,
    cfg: SolveConfig,
    *,
    attempts: int = 1,
    out_dir: Path,
    llm=chat,
    runner=run_task,
    journal=None,
) -> list[AttemptResult]:
    """pass@k-кампания: attempts независимых попыток решить задачу."""
    results = [
        solve_attempt(
            task, cfg, attempt=n, out_dir=out_dir, llm=llm, runner=runner, journal=journal
        )
        for n in range(1, attempts + 1)
    ]
    if journal:
        for r in results:
            journal(
                "attempt_result",
                {
                    "task": r.task,
                    "attempt": r.attempt,
                    "solved": r.solved,
                    "iterations": r.iterations,
                    "model": cfg.model,
                    "duration_sec": r.duration_sec,
                    "error": r.error,
                },
            )
    return results
