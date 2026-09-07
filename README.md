# ironharness

> harness — «упряжь»: впрягаем LLM-агентов в железо.

Агентский харнесс для I/O и прошивок. Два модуля:

- **io-core** — безопасный I/O-слой для агентов: транспорты (serial, Modbus TCP, MQTT,
  файловая песочница), симулятор Modbus, инструменты прошивки ESP32 (esptool: разбор образа
  офлайн, flash/erase на живой плате), JSONL-журнал всех операций, реплеер, лимиты
  (rate-limit, дедлайны), верификация эффектов (`expect_read`), MCP-сервер (19 инструментов).
- **ironbench** — бенчмарк для firmware-агентов: золотые задачи в симуляторах
  (Wokwi ESP32/MicroPython, далее Renode), агентский цикл поверх LLM API, отчёты pass@k.

## Быстрый старт

```bash
uv sync                              # зависимости (+ сам проект editable)
uv run pytest                        # тесты (без железа: loop:// и симуляторы)
uv run ruff check .                  # линтер
uv run ironharness-mcp               # MCP-сервер (stdio; или: python -m io_core.mcp_server)
```

## Инструменты агента (MCP)

`echo` · `serial_open/write/read/read_line` · `modbus_open/read/write` ·
`mqtt_open/publish/subscribe/read` · `esp_image_info/flash/erase` · `file_write/read/list/delete`

Все операции автоматически пишутся в JSONL-журнал (`$IRONHARNESS_HOME/journal.jsonl`,
по умолчанию `~/.ironharness/`); файловые операции изолированы песочницей
(`$IRONHARNESS_SANDBOX`, по умолчанию `~/.ironharness/sandbox`).

## Подключение внешнего агента

Любой MCP-совместимый агент (Claude Code, Codex, Cursor, OpenCode…) получает все
инструменты io-core одной записью в конфиг — свой цикл агент приносит с собой,
ironharness даёт «руки»: транспорты, песочницу, журнал, верификацию.

```json
{
  "mcpServers": {
    "ironharness": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ironharness", "ironharness-mcp"]
    }
  }
}
```

После публикации пакета на PyPI то же самое одной строкой: `"command": "uvx", "args": ["ironharness-mcp"]`.

Для испытаний на ненадёжных линиях есть `io_core.faults.FaultyTransport` — сценарные
сбои (обрыв, задержка, порча и потеря байтов) поверх любого транспорта, и
`io_core.mqtt_sim.MqttSimBroker` — мини-брокер MQTT (подмножество) для офлайн-прогонов.

## ironbench — бенчмарк firmware-агентов

```bash
uv run ironbench list                              # каталог золотых задач
uv run ironbench run --all                         # эталонные прогоны (нужен WOKWI_CLI_TOKEN)
uv run ironbench solve --task blink --attempts 3   # LLM-агент решает задачу
uv run ironbench report                            # pass@k: report.json + report.html
```

Задачи — ESP32/MicroPython в Wokwi (headless `wokwi-cli`), Renode, MicroPython unix-port
(бесплатные локальные прогоны) и plant-мишень (замкнутая петля «объект + регулятор» в
Python, оценка по метрикам переходной характеристики: p-regulator, pid-antiwindup,
system-id). У каждой задачи класс (io/data/protocol/fsm/control/resilience) и уровень
1–5; `ironbench report` показывает профиль модели по классам, а не одно число.
LLM-конфиг — переменные окружения: `LLM_BASE_URL` (по умолчанию локальный LM Studio),
`LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT`.

Статус: этапы 0–2 завершены (20 золотых задач на 4 мишенях, агентский цикл, таксономия,
отчёты pass@k + профиль по классам), далее — этап 3 (реальное железо, плата заказана).
План — `PLAN.md` (локально).
