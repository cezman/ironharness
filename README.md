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

Статус: этапы 0–1 завершены (CI-пайплайн активируется при пуше), далее ironbench (этап 2).
План — `PLAN.md` (локально, не пушится).
