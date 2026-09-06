# ironharness

> harness — «упряжь»: впрягаем LLM-агентов в железо.

Агентский харнесс для I/O и прошивок. Два модуля:

- **io-core** — безопасный I/O-слой для агентов: транспорты (serial, Modbus TCP, файловая
  песочница), симулятор Modbus, JSONL-журнал всех операций, реплеер, лимиты (rate-limit,
  дедлайны), верификация эффектов (`expect_read`), MCP-сервер (12 инструментов).
- **ironbench** — бенчмарк для firmware-агентов: золотые задачи в симуляторах
  (Wokwi ESP32/MicroPython, далее Renode), агентский цикл поверх LLM API, отчёты pass@k.

## Быстрый старт

```bash
uv sync                              # зависимости (+ сам проект editable)
uv run pytest                        # тесты (без железа: loop:// и симуляторы)
uv run ruff check .                  # линтер
uv run python -m io_core.mcp_server  # MCP-сервер (stdio)
```

## Инструменты агента (MCP)

`echo` · `serial_open/write/read/read_line` · `modbus_open/read/write` · `file_write/read/list/delete`

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
      "args": ["run", "--directory", "/path/to/ironharness", "python", "-m", "io_core.mcp_server"]
    }
  }
}
```

Для испытаний на ненадёжных линиях есть `io_core.faults.FaultyTransport` — сценарные
сбои (обрыв, задержка, порча и потеря байтов) поверх любого транспорта.

## ironbench — бенчмарк firmware-агентов

```bash
uv run ironbench list                              # каталог золотых задач
uv run ironbench run --all                         # эталонные прогоны (нужен WOKWI_CLI_TOKEN)
uv run ironbench solve --task blink --attempts 3   # LLM-агент решает задачу
uv run ironbench report                            # pass@k: report.json + report.html
```

Задачи — ESP32/MicroPython в Wokwi (headless `wokwi-cli`): blink → UART-эхо → антидребезг →
опрос датчика → командный протокол → конечный автомат → протокол с повторами.
LLM-конфиг — переменные окружения: `LLM_BASE_URL` (по умолчанию локальный LM Studio),
`LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT`.

Статус: этапы 0–2 завершены (бенчмарк: задачи, агентский цикл, отчёт pass@k), далее —
опциональный Renode-бэкенд и этап 3 (реальное железо). План — `PLAN.md` (локально).
