"""Раннер задач ironbench: мишени wokwi (облако) и renode (локально в WSL2);
real (этап 3) даёт честный FAIL. serial-лог → оценка по паттернам.

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

Мишень renode: MicroPython крутится в Renode под WSL2 (litex_vexriscv, ELF см.
tasks/_firmware). UART подключается к TCP-терминалу Renode; раннер сам говорит
по сокету тот же REPL-протокол (nudge → Ctrl+E paste-mode → код → Ctrl+D →
stimulus) и пишет serial-лог. Задачи объявляют мишень секцией renode в task.yaml
(platform/firmware/uart). Из Windows порт Renode доступен напрямую
(localhost-forwarding WSL2). Файлы стейджа уходят в WSL tar-потоком через stdin,
потому что drvfs-автомонтирование в дистрибутиве выключено.

Ограничение закреплённого litex-ELF (v1.11, 2019): куча ~2 КБ, нет machine/time/
input/sys.stdin — под ним идут только задачи «печать без ввода». Задачи с GPIO
остаются на wokwi; свежая сборка MicroPython для litex — в бэклоге (PLAN.md).
"""

from __future__ import annotations

import dataclasses
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
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

# --- мишень renode ---

# Порт socket-терминала по умолчанию: в WSL-режиме это лишь заполнитель в .resc —
# конвейер подставляет свободный порт (RENODE_PORT=...) и раннер подключается к нему;
# в тестовом режиме (инъекция команды) порт берётся из IRONBENCH_RENODE_PORT
RENODE_PORT = 3456

# Дедлайны стен часов: старт Renode+Mono занимает секунды, paste-mode отвечает сразу.
# Модульные константы — тесты подменяют их, чтобы не ждать по-настоящему.
RENODE_CONNECT_SEC = 20
RENODE_STEP_SEC = 10

# Куда в WSL2 складывается стейдж задачи (внутри дистрибутива drvfs выключен)
RENODE_REMOTE_ROOT = "$HOME/ironharness-runs"


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
    renode_cmd: str | list | None = None,
    unix_cmd: str | list | None = None,
    journal=None,
) -> TaskResult:
    """Диспетчер мишеней: wokwi/renode/unix реализованы, real — честный FAIL.

    cli_path/token/renode_cmd/unix_cmd — точки инъекции для тестов (фейковые
    CLI вместо реальных). journal — io_core.JsonlJournal: пишем task_start/task_result.
    """
    if task.target == "wokwi":
        return _run_wokwi(task, out_dir=out_dir, cli_path=cli_path, token=token, journal=journal)
    if task.target == "renode":
        return _run_renode(task, out_dir=out_dir, renode_cmd=renode_cmd, journal=journal)
    if task.target == "unix":
        return _run_unix(task, out_dir=out_dir, unix_cmd=unix_cmd, journal=journal)
    result = TaskResult(
        task=task.name,
        passed=False,
        exit_code=None,
        duration_sec=0.0,
        serial_log=None,
        missed=tuple(task.expect),
        error=(
            "мишень 'real' не реализована (этап 3: нужны живая плата (usbipd) и бэкенд real); "
            "проверки не выполнялись"
        ),
    )
    _journal_result(journal, result)
    return result


def is_infra_error(error: str | None) -> bool:
    """Инфраструктурный сбой среды (агент не может его исправить) — для раннего
    выхода из solve-цикла, чтобы не жечь LLM-итерации на неисправимой ошибке."""
    if not error:
        return False
    marks = (
        "не реализована",
        "не найден",
        "не удалось подготовить задачу",
        "не удалось подключиться",
        "не отвечает",
        "paste mode",
    )
    return any(m in error for m in marks)


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


# --- мишень renode: Renode в WSL2, UART → TCP-терминал ---


def _parse_delay(value: str) -> float:
    """'1500ms' → 1.5, '2s' → 2.0; без суффикса — секунды."""
    text = str(value).strip().lower()
    if text.endswith("ms"):
        return int(text[:-2]) / 1000
    if text.endswith("s"):
        return float(text[:-1])
    return float(text)


def generate_renode_resc(task: Task, port: int) -> str:
    """Скрипт Renode: платформа из поставки, UART на TCP-терминал, прошивка.

    __FIRMWARE__ подменяет sed внутри WSL на абсолютный путь стейджа (Python не
    знает $HOME дистрибутива); маркер пути @ остаётся в шаблоне — sed съедал его
    вместе с плейсхолдером @FIRMWARE@, и монитору уходил путь без @. Имя
    UART-периферии берётся из секции renode.
    """
    uart = task.renode.get("uart", "uart")
    return f""":name: ironbench {task.name}
using sysbus
mach create
machine LoadPlatformDescription @platforms/cpus/{task.renode['platform']}.repl
emulation CreateServerSocketTerminal {port} "term"
connector Connect sysbus.{uart} term
sysbus LoadELF @__FIRMWARE__
start
"""


def _stage_renode_task(task: Task, out_dir: Path, port: int) -> Path:
    """Стейдж задачи для renode: .resc и wsl-run.sh (едут в WSL), локальные
    копии — для разбора полётов. Прошивку возим отдельно (_push_firmware):
    большие блобы через stdin-релей wsl.exe доходят битыми."""
    if not task.renode.get("platform") or not task.renode.get("firmware"):
        raise ValueError("в секции renode нужны platform и firmware")
    stage = out_dir / task.name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    entry = task.directory / task.entry
    if not entry.is_file():
        raise ValueError(f"entry-файл не найден: {entry}")
    firmware = task.renode["firmware"]
    firmware_src = task.directory / firmware
    if not firmware_src.is_file():
        firmware_src = FIRMWARE_DIR / firmware
    if not firmware_src.is_file():
        raise ValueError(f"прошивка {firmware!r} не найдена ни в задаче, ни в tasks/_firmware/")
    (stage / "renode.resc").write_text(
        generate_renode_resc(task, port), encoding="utf-8", newline="\n"
    )
    (stage / "wsl-run.sh").write_text(_wsl_run_script(task), encoding="utf-8", newline="\n")
    return stage


def _wsl_run_script(task: Task) -> str:
    """wsl-run.sh: вся логика запуска Renode внутри WSL. Живёт файлом (едет в
    крошечном tar стейджа), поэтому не зависит от капризов передачи argv через
    wsl.exe. Порт выбирается свободный: порт прошлого прогона может держать
    zombie-слушатель WSL2-релея. pkill -9 обязателен: mono умирает от SIGTERM
    дольше секунды и не отпускает порт; шаблон ловит пути бинарника (symlink и
    portable), но не наши каталоги/файлы с именем renode-*."""
    renode_bin = os.environ.get("IRONBENCH_RENODE_BIN", "~/renode/renode")
    firmware = task.renode["firmware"]
    script = f"""#!/bin/bash
cd "$(dirname "$0")"
pkill -9 -f 'renode/renode|renode_[0-9]' 2>/dev/null
for i in $(seq 20); do pgrep -f 'renode/renode|renode_[0-9]' >/dev/null 2>&1 || break; sleep 0.5; done
PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('0.0.0.0',0)); print(s.getsockname()[1])")
sed -i "s|__FIRMWARE__|$HOME/ironharness-firmware/{firmware}|; s/CreateServerSocketTerminal [0-9][0-9]*/CreateServerSocketTerminal $PORT/" renode.resc
echo "RENODE_PORT=$PORT"
sleep infinity | {renode_bin} --disable-xwt --console renode.resc > run.log 2>&1
"""
    return script


def _wsl_distro() -> str:
    return os.environ.get("IRONBENCH_RENODE_DISTRO", "OpenClawGateway")


def _tar_of(path: Path, arcname: str) -> bytes:
    """Один файл tar.gz-блобом (для передачи в stdin WSL через communicate)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(path, arcname=arcname)
    return buf.getvalue()


def _push_to_wsl(blob: bytes, remote_dir: str, marker: str) -> None:
    """tar.gz-блоб в stdin WSL (через communicate: relay wsl.exe надёжен только
    так); маркер в stdout подтверждает распаковку."""
    try:
        proc = subprocess.run(
            [
                "wsl",
                "-d",
                _wsl_distro(),
                "--",
                "bash",
                "-c",
                f"mkdir -p {remote_dir} && tar -xzf - -C {remote_dir} && echo {marker}",
            ],
            input=blob,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ConnectionError("WSL не ответил при передаче файлов (таймаут 120 c)") from None
    if marker not in proc.stdout.decode("utf-8", "replace"):
        raise ConnectionError(
            f"не удалось передать файлы в WSL ({remote_dir}): "
            + (proc.stdout + proc.stderr).decode("utf-8", "replace")[-300:]
        )


def _push_firmware(task: Task) -> None:
    """Докладывает прошивку в постоянный каталог WSL ~/ironharness-firmware,
    откуда её читает wsl-run.sh (путь подставляет sed в .resc)."""
    firmware = task.renode["firmware"]
    src = task.directory / firmware
    if not src.is_file():
        src = FIRMWARE_DIR / firmware
    if not src.is_file():
        raise ValueError(f"прошивка {firmware!r} не найдена ни в задаче, ни в tasks/_firmware/")
    _push_to_wsl(_tar_of(src, src.name), "$HOME/ironharness-firmware", "FW-PUSHED")


def _wsl_renode_cmd(remote_dir: str) -> list[str]:
    """Запуск wsl-run.sh из стейджа; stdin свободен (DEVNULL), логи — в stdout."""
    return ["wsl", "-d", _wsl_distro(), "--", "bash", "-c", f"bash {remote_dir}/wsl-run.sh"]


class _TelnetFilter:
    """Срезает telnet-IAC-последовательности: socket-терминал Renode — telnet-сервер
    и присылает согласование (IAC WILL/DO...) и экранирует байты 0xFF в данных.
    Неполная IAC-последовательность на границе чанков ждёт продолжения в
    следующем чанке (хвост буфера живёт между feed)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._iac = False  # ждём байт-команду после IAC
        self._sub = False  # внутри IAC SB ... IAC SE

    def feed(self, data: bytes) -> str:
        self._buf += data
        out = bytearray()
        i = 0
        n = len(self._buf)
        while i < n:
            b = self._buf[i]
            if self._sub:
                if b == 0xFF:
                    if i + 1 >= n:  # неполно: ждём продолжения
                        break
                    if self._buf[i + 1] == 0xF0:  # IAC SE — конец поднеготиации
                        self._sub = False
                    i += 2  # IAC SE, экранированный 0xFF или мусор внутри SB
                else:
                    i += 1
            elif self._iac:
                if b in (0xFB, 0xFC, 0xFD, 0xFE):  # WILL/WONT/DO/DONT + байт-опция
                    if i + 1 >= n:  # опции ещё нет — ждём продолжения
                        break
                    i += 2
                    self._iac = False
                elif b == 0xFA:  # SB: поднеготиация до IAC SE
                    self._sub = True
                    i += 1
                    self._iac = False
                elif b == 0xFF:  # экранированный литеральный 0xFF
                    out.append(0xFF)
                    i += 1
                    self._iac = False
                else:  # NOP/GA и прочие без аргументов
                    i += 1
                    self._iac = False
            elif b == 0xFF:
                self._iac = True
                i += 1
            else:
                out.append(b)
                i += 1
        del self._buf[:i]
        return out.decode("utf-8", "replace")


def _recv_until(
    sock: socket.socket,
    needles: tuple[str, ...],
    deadline: float,
    tel: _TelnetFilter,
) -> tuple[str, bool]:
    """Читает сокет (срезая telnet-IAC), пока не встретится один из needles
    или не истечёт deadline."""
    buf = ""
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, min(0.2, deadline - time.monotonic())))
        try:
            data = sock.recv(4096)
        except TimeoutError:
            continue
        except OSError:
            break
        if not data:
            break
        buf += tel.feed(data)
        if any(n in buf for n in needles):
            return buf, True
    return buf, False


def _paste_code(code: str) -> str:
    """Убирает полные строковые комментарии: paste-mode шлёт исходник целиком,
    а UART-FIFO эмуляции конечен — каждый лишний байт повышает риск обрыва.
    Строки кода не трогаем (комментарии в хвостах строк остаются)."""
    return "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))


def _send_chunked(sock: socket.socket, data: bytes, chunk: int = 32, pause: float = 0.05) -> None:
    """Паста порциями: у легаси paste-mode нет flow control, FIFO emулятора
    переполняется при заливке одним куском."""
    for i in range(0, len(data), chunk):
        sock.sendall(data[i : i + chunk])
        time.sleep(pause)


def _drive_repl(sock: socket.socket, task: Task, wall_deadline: float) -> tuple[str, str | None]:
    """Говорит с MicroPython REPL по сокету: paste кода entry, stimulus, сбор serial.

    Возвращает (serial-текст, ошибка|None). Протокол повторяет paste-сценарий
    wokwi, но raw-paste прошивкой litex не поддержан — используется legacy
    paste mode (Ctrl+E, ответ 'paste mode; Ctrl-C to cancel...').
    """
    parts: list[str] = []
    tel = _TelnetFilter()
    # будим REPL пустой строкой: приглашение печатается раз, легко пропустить его при коннекте
    sock.sendall(b"\n")
    buf, ok = _recv_until(sock, (REPL_PROMPT,), wall_deadline, tel)
    parts.append(buf)
    if not ok:
        return "".join(parts), "REPL не отвечает (нет приглашения '>>>')"

    sock.sendall(b"\x05")
    buf, ok = _recv_until(
        sock, ("paste mode",), min(wall_deadline, time.monotonic() + RENODE_STEP_SEC), tel
    )
    parts.append(buf)
    if not ok:
        return "".join(parts), "paste mode недоступен (прошивка без Ctrl+E)"

    code = (task.directory / task.entry).read_text(encoding="utf-8")
    _send_chunked(sock, _paste_code(code).encode("utf-8") + b"\n\x04")

    # шаги stimulus; set-control (кнопки Wokwi) под renode не воспроизводим
    for step in task.stimulus:
        if time.monotonic() > wall_deadline:
            break
        if "set-control" in step:
            return "".join(parts), f"шаг set-control не поддержан мишенью renode: {step}"
        if "delay" in step:
            time.sleep(min(_parse_delay(step["delay"]), max(0.0, wall_deadline - time.monotonic())))
        elif "write-serial" in step:
            sock.sendall(str(step["write-serial"]).encode("utf-8"))
        elif "wait-serial" in step:
            buf, _ = _recv_until(sock, (str(step["wait-serial"]),), wall_deadline, tel)
            parts.append(buf)

    # дочитываем вывод: до полного набора литеральных expect (досрочный выход,
    # как wait-serial в wokwi-сценарии), до возврата приглашения (конечная
    # программа завершилась) или до дедлайна (бесконечный цикл прошивки)
    plain = tuple(p for p in task.expect if _plain_text(p))
    buf = ""
    while time.monotonic() < wall_deadline:
        sock.settimeout(max(0.05, min(0.5, wall_deadline - time.monotonic())))
        try:
            data = sock.recv(4096)
        except TimeoutError:
            continue
        except OSError:
            break
        if not data:
            break
        buf += tel.feed(data)
        if (plain and all(p in buf for p in plain)) or REPL_PROMPT in buf:
            break
    parts.append(buf)
    return "".join(parts), None


def _read_port_line(stream, timeout: float) -> int:
    """Читает stdout WSL-конвейера до строки RENODE_PORT=<n> (в отдельном
    потоке: readline блокирует, а конвейер может умереть до эха порта).
    Строки до порта (например, служебные сообщения wsl.exe в stderr, который
    мержится в stdout) пропускаются."""
    box: dict[str, int] = {}

    def reader():
        try:
            for line in iter(stream.readline, b""):
                if line.startswith(b"RENODE_PORT="):
                    raw = line.decode("utf-8", "replace").strip().split("=", 1)[1]
                    if raw.isdigit():  # мусор после '=' — читаем дальше
                        box["port"] = int(raw)
                        return
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if "port" not in box:
        raise ConnectionError("WSL-конвейер не сообщил порт (RENODE_PORT=...)")
    return box["port"]


def _run_renode(
    task: Task,
    *,
    out_dir: Path,
    renode_cmd: str | list | None = None,
    journal=None,
) -> TaskResult:
    """Запускает задачу в Renode и возвращает результат (pass/fail + причина).

    renode_cmd=None → стандартный WSL-путь (прошивка и стейдж уходят tar-блобами
    через communicate, затем запускается wsl-run.sh и сообщает порт);
    инъекция команды (тесты) запускает «Renode» локально, без WSL.
    """
    if "set-control" in {k for step in task.stimulus for k in step}:
        result = TaskResult(
            task=task.name,
            passed=False,
            exit_code=None,
            duration_sec=0.0,
            serial_log=None,
            missed=tuple(task.expect),
            error="мишень renode не поддерживает set-control (кнопки Wokwi)",
        )
        _journal_result(journal, result)
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{task.name}.serial.log"
    port = int(os.environ.get("IRONBENCH_RENODE_PORT", RENODE_PORT))
    wall_timeout = task.timeout_sec * 2 + WALL_GRACE_SEC
    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    serial_text = ""
    proc = None
    try:
        stage = _stage_renode_task(task, out_dir, port)
        if renode_cmd is None:
            _push_firmware(task)
            _push_to_wsl(
                _tar_of_files(stage), f"{RENODE_REMOTE_ROOT}/{task.name}", "STAGE-PUSHED"
            )
            cmd = _wsl_renode_cmd(f"{RENODE_REMOTE_ROOT}/{task.name}")
        else:
            cmd = [renode_cmd] if isinstance(renode_cmd, str) else list(renode_cmd)
        if journal:
            journal("task_start", {"task": task.name})
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if renode_cmd is None:
            # wsl-run.sh сам выбирает свободный порт и сообщает его
            port = _read_port_line(proc.stdout, timeout=RENODE_CONNECT_SEC)
        # ждем TCP-терминал: старт Renode+Mono в WSL занимает секунды
        sock = None
        connect_deadline = time.monotonic() + RENODE_CONNECT_SEC
        while sock is None:
            try:
                sock = socket.create_connection(("localhost", port), timeout=1.0)
            except OSError:
                if proc.poll() is not None or time.monotonic() > connect_deadline:
                    raise ConnectionError(
                        f"socket-терминал Renode на :{port} недоступен"
                    ) from None
                time.sleep(0.2)
        with sock:
            serial_text, error = _drive_repl(sock, task, time.monotonic() + wall_timeout)
        exit_code = 0 if error is None else None
    except ValueError as e:
        error = f"не удалось подготовить задачу: {e}"
    except ConnectionError as e:
        error = f"не удалось подключиться: {e}"
    except FileNotFoundError as e:
        error = f"не найден: {e.filename or e}"
    except OSError as e:
        error = f"ошибка ввода-вывода при запуске Renode: {e}"
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()

    serial_log.write_text(serial_text, encoding="utf-8")
    duration = round(time.monotonic() - start, 2)
    missed, hit_fail = _check_patterns(serial_text, task.expect, task.fail)
    passed = not missed and not hit_fail and error is None
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


def _tar_of_files(stage: Path) -> bytes:
    """Все файлы стейджа одним tar.gz-блобом (крошечный: resc + wsl-run.sh)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(stage.iterdir()):
            tar.add(item, arcname=item.name)
    return buf.getvalue()


# --- мишень unix: MicroPython unix-port в WSL2 (бесплатные локальные прогоны) ---


def _unix_cmd(remote_entry: str) -> list[str]:
    """micropython исполняет entry напрямую: stdin = stimulus, stdout = serial-лог."""
    upy_bin = os.environ.get("IRONBENCH_UNIX_BIN", "~/bin/micropython")
    return ["wsl", "-d", _wsl_distro(), "--", "bash", "-c", f"exec {upy_bin} {remote_entry}"]


def _run_unix(
    task: Task,
    *,
    out_dir: Path,
    unix_cmd: str | list | None = None,
    journal=None,
) -> TaskResult:
    """Запускает entry MicroPython-ом unix-port и оценивает вывод.

    Без REPL и paste: скрипт исполняется файлом, input() читает наш stdin —
    значит годятся только чисто-serial задачи (machine/dht недоступны).
    Enter в unix — \\n, поэтому \\r из wokwi-стимула переводится в \\n.
    unix_cmd=None → WSL-путь (entry уезжает tar-блобом); инъекция команды —
    локальный фейк для тестов.
    """
    if "set-control" in {k for step in task.stimulus for k in step}:
        result = TaskResult(
            task=task.name,
            passed=False,
            exit_code=None,
            duration_sec=0.0,
            serial_log=None,
            missed=tuple(task.expect),
            error="мишень unix не поддерживает set-control (кнопки/датчики Wokwi)",
        )
        _journal_result(journal, result)
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    serial_log = out_dir / f"{task.name}.serial.log"
    wall_timeout = task.timeout_sec * 2 + WALL_GRACE_SEC
    start = time.monotonic()
    exit_code: int | None = None
    error: str | None = None
    serial_text = ""
    proc = None
    try:
        if unix_cmd is None:
            remote_dir = f"{RENODE_REMOTE_ROOT}/{task.name}-unix"
            _push_to_wsl(
                _tar_of(task.directory / task.entry, task.entry), remote_dir, "STAGE-PUSHED"
            )
            cmd = _unix_cmd(f"{remote_dir}/{task.entry}")
        else:
            cmd = [unix_cmd] if isinstance(unix_cmd, str) else list(unix_cmd)
        if journal:
            journal("task_start", {"task": task.name})
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # traceback'и unix-port пишет в stderr
        )
        box = {"text": "", "eof": False}

        def reader():
            try:
                for line in iter(proc.stdout.readline, b""):
                    box["text"] += line.decode("utf-8", "replace")
            except OSError:
                pass
            finally:
                box["eof"] = True

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.monotonic() + wall_timeout
        plain = tuple(p for p in task.expect if _plain_text(p))
        try:
            for step in task.stimulus:
                if time.monotonic() > deadline or box["eof"]:
                    break
                if "delay" in step:
                    time.sleep(
                        min(_parse_delay(step["delay"]), max(0.0, deadline - time.monotonic()))
                    )
                elif "wait-serial" in step:
                    needle = str(step["wait-serial"])
                    while (
                        needle not in box["text"]
                        and time.monotonic() < deadline
                        and not box["eof"]
                    ):
                        time.sleep(0.05)
                elif "write-serial" in step:
                    assert proc.stdin is not None
                    data = str(step["write-serial"]).replace("\r\n", "\n").replace("\r", "\n")
                    proc.stdin.write(data.encode("utf-8"))
                    proc.stdin.flush()
            # дочитываем: до всех литеральных expect, EOF или дедлайна
            # (бесконечный цикл прошивки — норма, как --timeout в wokwi)
            while time.monotonic() < deadline and not box["eof"]:
                if plain and all(p in box["text"] for p in plain):
                    break
                time.sleep(0.05)
            matched = bool(plain) and all(p in box["text"] for p in plain)
            if box["eof"]:
                exit_code = proc.wait(timeout=5)
            elif matched or time.monotonic() >= deadline:
                # гасим сразу, не закрывая stdin: EOF у input() бесконечной
                # прошивки дал бы Traceback в serial-логе (fail-паттерн)
                proc.kill()
                proc.wait(timeout=5)
                exit_code = None
            else:
                try:
                    exit_code = proc.wait(timeout=3)  # конечная программа сама выйдет
                except subprocess.TimeoutExpired:
                    exit_code = None
            serial_text = box["text"]
            if exit_code not in (0, None):
                error = f"micropython завершился с кодом {exit_code}"
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
    except ValueError as e:
        error = f"не удалось подготовить задачу: {e}"
    except ConnectionError as e:
        error = f"не удалось подключиться: {e}"
    except FileNotFoundError as e:
        error = f"не найден: {e.filename or e}"
    except OSError as e:
        error = f"ошибка ввода-вывода при запуске micropython: {e}"
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()

    serial_log.write_text(serial_text, encoding="utf-8")
    duration = round(time.monotonic() - start, 2)
    missed, hit_fail = _check_patterns(serial_text, task.expect, task.fail)
    passed = not missed and not hit_fail and error is None
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
