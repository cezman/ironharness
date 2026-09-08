# ironharness

[English](README.md) | [Русский](README.ru.md)

> we harness LLM agents to hardware.

An agent harness for I/O and firmware. Two modules:

- **io-core** — a safe I/O layer for agents: transports (serial, Modbus TCP, MQTT,
  file sandbox), a Modbus simulator, ESP32 flashing tools (esptool: offline image
  inspection, flash/erase on a live board), a JSONL journal of every operation, a
  replayer, limits (rate limit, deadlines), effect verification (`expect_read`), and
  an MCP server (22 tools).
- **ironbench** — a benchmark for firmware agents: golden tasks in simulators
  (Wokwi ESP32/MicroPython, plus Renode), an agent loop over any LLM API, pass@k reports.

## Quick start

```bash
uvx ironharness-mcp                  # run the MCP server (no install)
```

From source:

```bash
uv sync                              # dependencies (+ the project itself, editable)
uv sync --extra flash                # + esptool (GPLv2+, kept out of the MIT core)
uv run pytest                        # tests (no hardware: loop:// and simulators)
uv run ruff check .                  # linter
uv run ironharness-mcp               # MCP server (stdio; or: python -m io_core.mcp_server)
```

## Agent tools (MCP)

`echo` · `serial_open/write/read/read_line/close` · `modbus_open/read/write/close` ·
`mqtt_open/publish/subscribe/read/close` · `esp_image_info/flash/erase` · `file_write/read/list/delete`

Every operation is journaled to JSONL (`$IRONHARNESS_HOME/journal.jsonl`,
default `~/.ironharness/`); file operations are confined by the sandbox
(`$IRONHARNESS_SANDBOX`, default `~/.ironharness/sandbox`).

## Connecting an external agent

Any MCP-compatible agent (Claude Code, Codex, Cursor, OpenCode…) gets all io-core
tools with one config entry — the agent brings its own loop, ironharness provides
the hands: transports, sandbox, journal, verification.

```json
{
  "mcpServers": {
    "ironharness": {
      "command": "uvx",
      "args": ["ironharness-mcp"]
    }
  }
}
```

For unreliable-line testing there is `io_core.faults.FaultyTransport` — scripted
failures (disconnect, delay, bit corruption, byte loss) over any transport — and
`io_core.mqtt_sim.MqttSimBroker`, a minimal MQTT broker for offline runs.

## ironbench — a benchmark for firmware agents

```bash
uv run ironbench list                              # catalog of golden tasks
uv run ironbench run --all                         # reference runs (needs WOKWI_CLI_TOKEN)
uv run ironbench solve --task blink --attempts 3   # an LLM agent solves a task
uv run ironbench report                            # pass@k: report.json + report.html
```

Tasks run on ESP32/MicroPython in Wokwi (headless `wokwi-cli`), Renode, the
MicroPython unix port (free local runs), and a plant target (a closed-loop
«object + controller» simulation scored on step-response metrics: p-regulator,
pid-antiwindup, system-id). Every task has a class (io/data/protocol/fsm/control/
resilience) and a level 1–5; `ironbench report` shows a model's profile across
classes, not a single number. LLM config — environment variables: `LLM_BASE_URL`
(default: local LM Studio), `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT`.

## Safety / intended use

- This is a **bench tool for developing and testing agents, not production
  middleware**. It is designed to be run locally against simulators and your own
  dev hardware.
- **MQTT transport is plaintext TCP** — no TLS, no authentication. Do not point it
  at production brokers or untrusted networks.
- **esp_flash/esp_erase modify real hardware** and are gated behind
  `IRONHARNESS_ALLOW_REAL_FLASH=1` (opt-in). erasing flash is irreversible
  (ESP32 recovers by reflashing, but data is gone). esptool is an optional
  dependency: `pip install 'ironharness[flash]'`.
- Transports are not restricted to specific hosts/ports by design — the operator
  (you) decides what the agent may reach; every operation is journaled for audit.
  Flash/erase failures are journaled with full details (`esp_flash_failed`) even
  when the MCP envelope truncates them.

## Status

MVP under active development. Example benchmark results live in [`reports/`](reports/)
— recorded runs (JSON + HTML, open in a browser).
