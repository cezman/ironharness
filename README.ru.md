# ironharness

[English](README.md) | [Русский](README.ru.md)

[![tests](https://github.com/cezman/ironharness/actions/workflows/tests.yml/badge.svg)](https://github.com/cezman/ironharness/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/ironharness)](https://pypi.org/project/ironharness/)
[![Python](https://img.shields.io/pypi/pyversions/ironharness)](https://pypi.org/project/ironharness/)
[![License](https://img.shields.io/pypi/l/ironharness)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Benchmark reports](https://img.shields.io/badge/benchmark%20reports-gh--pages-2ea44f)](https://cezman.github.io/ironharness/)

> harness — «упряжь»: впрягаем LLM-агентов в железо.

Агентский харнесс для I/O и прошивок. Два модуля:

- **io-core** — безопасный I/O-слой для агентов: транспорты (serial, Modbus TCP, MQTT,
  файловая песочница), симулятор Modbus, инструменты прошивки ESP32 (esptool: разбор образа
  офлайн, flash/erase на живой плате), JSONL-журнал всех операций, реплеер, лимиты
  (rate-limit, дедлайны), верификация эффектов (`expect_read`), MCP-сервер (33 инструмента).
- **ironbench** — бенчмарк для firmware-агентов: золотые задачи в симуляторах
  (Wokwi ESP32/MicroPython, плюс Renode), агентский цикл поверх LLM API, отчёты pass@k.

**Для кого.** Для разработчиков агентов, работающих с реальным железом — serial-приборы,
Modbus-устройства, платы класса ESP32 — и для всех, кто измеряет, что LLM-агент реально
может в прошивках: на симуляторе и на живой плате.

Док [«Зачем ironharness»](docs/why.ru.md) (по-английски:
[docs/why.md](docs/why.md)): проблема, десятиминутный старт без железа,
рецепты «ситуация → инструмент».

**Что измеряется и почему цифрам можно верить.** Задача считается решённой, только когда
*раннер* проверил наблюдаемое поведение прошивки (serial-протокол, тайминги пинов,
показания датчика) — а не когда строки похожи на правду. Скоринг стоит на античит-якорях
(штампы wait-serial, канонические вердикты, байтовые капы логов); каждая I/O-операция
попадает в JSONL-журнал; каждая попытка несёт структурный `error_kind` — инфраструктурные
отказы исключаются из оценок, а не раздувают их; вместе с каждым поколением лидерборда
публикуются сырые данные по попыткам, чтобы цифры можно было перепроверить.

PyPI-пакет содержит только слой **io-core** (MCP-сервер). Бенчмарк запускается из
клона исходников: `git clone` → `uv sync` → `uv run python -m ironbench.cli ...`.

## Быстрый старт

```bash
uvx --from ironharness ironharness-mcp   # MCP-сервер без установки
```

Из исходников:

```bash
uv sync                              # зависимости (+ сам проект editable)
uv sync --extra flash                # + esptool (GPLv2+, вынесена из MIT-ядра)
uv run pytest                        # тесты (без железа: loop:// и симуляторы)
uv run ruff check .                  # линтер
uv run ironharness-mcp               # MCP-сервер (stdio; или: python -m io_core.mcp_server)
```

## Инструменты агента (MCP)

`echo` · `serial_list` (порты с VID/PID) · `serial_wait` (блокируется до появления VID:PID) · `session_status` · `serial_open` (принимает `by-serial:<sn>`), `serial_write`, `serial_read`, `serial_read_line`, `serial_close` · `serial_put`, `serial_get` (перекачка файлов на плату и обратно через raw REPL) · `serial_reader_start`, `serial_reader_stop`, `serial_tail`, `serial_read_until` (фоновый ридер: вывод устройства между вызовами инструментов буферизуется, а не теряется) · `serial_monitor` (ограниченное окно захвата: капы по байтам/времени, quiet-окно, stop-паттерн, дамп в песочницу) · `serial_reset` (сброс платы импульсом RTS между попытками solve) · `modbus_open`, `modbus_read`, `modbus_write`, `modbus_close` ·
`mqtt_open`, `mqtt_publish`, `mqtt_subscribe`, `mqtt_read`, `mqtt_close` · `esp_image_info`, `esp_flash`, `esp_erase` · `file_write`, `file_read`, `file_list`, `file_delete`

Все операции автоматически пишутся в JSONL-журнал (`$IRONHARNESS_HOME/journal.jsonl`,
по умолчанию `~/.ironharness/`); файловые операции изолированы песочницей
(`$IRONHARNESS_SANDBOX`, по умолчанию `~/.ironharness/sandbox`). Конкурентная запись
безопасна: потоки одной сессии и отдельные процессы с общим `IRONHARNESS_HOME`
сериализуются sidecar-локом, а ротация по размеру (32 МБ, хранить 5 частей)
совместима с несколькими писателями.

## Подключение внешнего агента

Любой MCP-совместимый агент (Claude Code, Codex, Cursor, OpenCode…) получает все
инструменты io-core одной записью в конфиг — свой цикл агент приносит с собой,
ironharness даёт «руки»: транспорты, песочницу, журнал, верификацию.

```json
{
  "mcpServers": {
    "ironharness": {
      "command": "uvx",
      "args": ["--from", "ironharness", "ironharness-mcp"]
    }
  }
}
```

Готовый скилл для агента — установка и правила безопасной работы с железом — лежит
в [`skills/ironharness/SKILL.md`](skills/ironharness/SKILL.md): положите его в каталог
скиллов Claude Code или вставьте в инструкции любого агента. Живая демонстрация:
[стираем ESP32 и записываем метеостанцию обратно](https://cezman.github.io/ironharness/ops-demo.html) —
инструменты MCP против голых рук, журнал как доказательство.

Для испытаний на ненадёжных линиях есть `io_core.faults.FaultyTransport` — сценарные
сбои (обрыв, задержка, порча и потеря байтов) поверх любого транспорта, и
`io_core.mqtt_sim.MqttSimBroker` — мини-брокер (подмножество MQTT 3.1.1) для
офлайн-прогонов.

## ironbench — бенчмарк firmware-агентов

```bash
uv run python -m ironbench.cli list                              # каталог золотых задач
uv run python -m ironbench.cli run --task uart-echo              # один эталонный прогон (wokwi-задачам нужен WOKWI_CLI_TOKEN)
uv run python -m ironbench.cli run --all --allow-real            # все задачи одним проходом; без --allow-real
                                                                 #   отказ — стейджинг сносит main.py на живой плате
uv run python -m ironbench.cli solve --task blink --attempts 3   # LLM-агент решает задачу
uv run python -m ironbench.cli report                            # pass@k: report.json + report.html
```

Задачи — ESP32/MicroPython в Wokwi (headless `wokwi-cli`), Renode, MicroPython unix-port
(бесплатные локальные прогоны), plant-мишень (замкнутая петля «объект + регулятор» в
Python, оценка по метрикам переходной характеристики: p-regulator, pid-antiwindup,
system-id) и **живая плата** (`--target real`: MicroPython REPL по USB-UART, opt-in через
`IRONBENCH_REAL_PORT=COM4`). Одна и та же задача может пройти на симуляторе и на железе —
на этом контрасте `real` и построен; `bme-read` (датчик BME280 по I2C) оценивается только
на живой плате. Задача может нести экспертные `notes:` со стенда — они попадают в промпт
solve-агента, а факт «с notes / без» фиксируется в results.jsonl. У каждой задачи класс
(io/data/protocol/fsm/control/resilience/debug) и уровень 1–5; `python -m ironbench.cli report`
показывает профиль модели по классам, а не одно число. LLM-конфиг — переменные окружения:
`LLM_BASE_URL` (по умолчанию локальный LM Studio), `LLM_MODEL`, `LLM_API_KEY`,
`LLM_TIMEOUT`, `LLM_MAX_ITERATIONS`, `LLM_MAX_TOKENS` и `LLM_ALLOW_LOCAL`
(`0`, `false`, `no` запрещают локальные/приватные LLM-эндпоинты — граница
SSRF; любое другое значение разрешает).

Живые цифры: [лидерборд](https://cezman.github.io/ironharness/), архивные поколения
с зафиксированными критериями и сырыми данными по попыткам
([generations](https://cezman.github.io/ironharness/generations/)) и кейс-стади с живой
платы: [3 строки экспертных notes — 4/4 решений с первой итерации против 2/4 с полным
бюджетом в 10× дольше](https://cezman.github.io/ironharness/case-study-notes-ab.html).

## Безопасность / предназначение

- Это **стендовый инструмент для разработки и тестирования агентов, не прод-прослойка**.
  Рассчитан на локальную работу с симуляторами и своим dev-железом.
- **MQTT-транспорт — открытый TCP**: без TLS и аутентификации. Не направляйте его на
  продовые брокеры и недоверенные сети.
- **esp_flash/esp_erase меняют реальное железо** и закрыты флагом
  `IRONHARNESS_ALLOW_REAL_FLASH=1` (opt-in). Стирание флеша необратимо (ESP32
  восстанавливается перепрошивкой, но данные пропадают). esptool — опциональная
  зависимость: `pip install 'ironharness[flash]'`.
- **Код задач исполняется без песочницы**: локально запускаемый код задач — контроллеры
  plant (контроллеры plant-мишени и прошивка на unix/real-мишенях) — работает как локальные
  процессы с правами вашего пользователя (прошивка wokwi исполняется в облаке Wokwi).
  Граница процесса ограничивает только зависания и падения — это не файловая и не сетевая
  песочница. Контроллеры plant получают одноразовый рабочий каталог (относительные записи
  остаются внутри артефактов прогона), но могут читать и писать всё, что может пользователь.
  Запускайте только задачи авторов, которым доверяете; для враждебного кода — VM или контейнер.
- **Бенчмарк на живой плате — opt-in и ограничен**: `--target real` требует
  `IRONBENCH_REAL_PORT`; каждый прогон ограничен дедлайном; стейджинг задачи на плату
  заранее сохраняет `main.py` платы в артефакты прогона перед стиранием; любой
  real-прогон (`run`/`solve`) без `--allow-real` отказывается работать, а если проба
  не смогла подтвердить бэкап — стирание отменяется вовсе. Вернуть свою прошивку после
  прогона: прошейте `main.py.backup` из артефактов прогона обратно на плату
  (serial_put/ampy).
- **Известный caveat измерений**: serial-вывод прошивки возвращается в промпт решающей модели
  как фидбек. Он не может перевернуть вердикт PASS/FAIL (оценку ставит runner), но модель
  может быть поведена выводом собственной прошивки — принятая дисторсия бенчмарка.
- **Граница античита (wokwi)**: в serial-логе wokwi нет штампов времени, поэтому
  интерактивные задачи якорятся на эхо `input()` (ответ, напечатанный до первого
  стимула, ловится как «pre-printed»), а задачи без ввода остаются там
  незаякоренными — unix/real якорят всё. Остаток помечен на лидерборде
  (нота о неэквивалентности) и не скрывается; в опубликованных поколениях
  лидерборда wokwi-прогонов нет.
- **Serial-порты — белый список локальных портов** — `COM*`, `/dev/tty*`,
  `/dev/pts/*`, `loop://`, `pty://`. pyserial умеет и сетевые URL (`socket://host:port` —
  исходящий TCP, `rfc2217://` — удалённый serial поверх TCP) — `serial_open` такие
  отклоняет с журнальной записью `serial_open_failed`. Адреса Modbus/MQTT контролирует
  оператор через политику доступа ниже.
- Транспорты по умолчанию не ограничены по хостам/портам — что доступно агенту, решает
  оператор (вы), при желании — через политику доступа ниже; каждая операция журналуется
  для аудита.
- **Политика доступа (opt-in)**: `IRONHARNESS_ALLOWED_HOSTS` (список через запятую:
  `host` или `host:port`) разрешает Modbus/MQTT-подключения только к перечисленным
  хостам — остальное отклоняется с `PolicyViolation` до попытки соединения
  (`broker.lan:1883` — только этот порт, `broker.lan` без порта — любой).
  Хосты сравниваются как точные строки (без DNS-резолва; IPv6-записи пока не
  поддерживаются). `IRONHARNESS_ENABLED_KINDS` (список `serial,modbus,mqtt,esp,file` через запятую)
  выключает целые виды транспортов — запрещённые open/esp/file-операции падают сразу.
  Незаданные переменные сохраняют старое поведение «разрешено всё», каждый отказ
  журналуется событием `policy_violation`.

## Статус

MVP в активной разработке. Зафиксированные результаты бенчмарка живут на
[gh-pages](https://cezman.github.io/ironharness/): лидерборд, архивные поколения
с сырыми данными по попыткам и кейс-стади экспертных notes.
