# ironharness

[English](README.md) | [Русский](README.ru.md)

[![tests](https://github.com/cezman/ironharness/actions/workflows/tests.yml/badge.svg)](https://github.com/cezman/ironharness/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/ironharness)](https://pypi.org/project/ironharness/)
[![Python](https://img.shields.io/pypi/pyversions/ironharness)](https://pypi.org/project/ironharness/)
[![License](https://img.shields.io/pypi/l/ironharness)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Benchmark reports](https://img.shields.io/badge/benchmark%20reports-gh--pages-2ea44f)](https://cezman.github.io/ironharness/)

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
uvx --from ironharness ironharness-mcp   # run the MCP server (no install)
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

`echo` · `serial_open/write/read/read_line/close` · `serial_reader_start/stop`, `serial_tail`, `serial_read_until` (background reader: device output between tool calls is buffered, not lost) · `modbus_open/read/write/close` ·
`mqtt_open/publish/subscribe/read/close` · `esp_image_info/flash/erase` · `file_write/read/list/delete`

Every operation is journaled to JSONL (`$IRONHARNESS_HOME/journal.jsonl`,
default `~/.ironharness/`); file operations are confined by the sandbox
(`$IRONHARNESS_SANDBOX`, default `~/.ironharness/sandbox`). One journal
writer per file: concurrent threads of a session are safe, but two processes
sharing one `IRONHARNESS_HOME` can silently drop lines (keep one agent per
home directory).

## Connecting an external agent

Any MCP-compatible agent (Claude Code, Codex, Cursor, OpenCode…) gets all io-core
tools with one config entry — the agent brings its own loop, ironharness provides
the hands: transports, sandbox, journal, verification.

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
MicroPython unix port (free local runs), a plant target (a closed-loop
«object + controller» simulation scored on step-response metrics: p-regulator,
pid-antiwindup, system-id), and a **live board** (`--target real`: MicroPython
REPL over USB-UART, opt-in via `IRONBENCH_REAL_PORT=COM4`). The same task can
be run on a simulator and on hardware — that contrast is what `real` is for.
Every task has a class (io/data/protocol/fsm/control/
resilience/debug) and a level 1–5; `ironbench report` shows a model's profile across
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
- **Task code runs unsandboxed**: locally executed task code - plant controllers
  (`python -m ironbench.plant`) and firmware on the unix/real targets - runs as
  local processes with your user's rights (wokwi firmware runs in the Wokwi
  cloud instead). A process boundary bounds hangs and crashes only - it is not a
  filesystem or network sandbox. Plant controllers get a throwaway working
  directory (relative writes stay inside the run artifacts), but they can read
  or write anything the user can. Only run tasks from authors you trust; for
  hostile code use a VM or a container.
- **Known measurement caveat**: the firmware's serial output is fed back into
  the solving model's prompt as feedback. It cannot flip the PASS/FAIL verdict
  (scoring is done by the runner), but a model can be steered by its own
  firmware's output - an accepted distortion of the benchmark.
- Transports are not restricted to specific hosts/ports by default — the operator
  (you) decides what the agent may reach, optionally via the access policy below;
  every operation is journaled for audit. Flash/erase failures are journaled with
  full details (`esp_flash_failed`) even when the MCP envelope truncates them.
- **Access policy (opt-in)**: set `IRONHARNESS_ALLOWED_HOSTS` (comma-separated
  `host` or `host:port` entries) to restrict Modbus/MQTT connections to the
  listed hosts — anything else is denied with `PolicyViolation` before a
  connection is attempted (`broker.lan:1883` matches that exact port, a bare
  `broker.lan` matches any port). Hosts match as exact strings (no DNS
  resolution; IPv6 entries are not supported yet). Set `IRONHARNESS_ENABLED_KINDS` (comma list of
  `serial,modbus,mqtt,esp,file`) to disable whole transport kinds — disabled
  open/esp/file operations fail fast. Unset variables keep the allow-everything
  default, and every denial is journaled as a `policy_violation` event.

## Status

MVP under active development. Example benchmark results live in [`reports/`](reports/)
— recorded runs (JSON + HTML, open in a browser).
