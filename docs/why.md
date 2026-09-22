# Why ironharness? A field guide

This page is for humans deciding whether they need this MCP server. It answers
three questions: *what problem does it solve*, *how do I touch it in ten
minutes without any hardware*, and *I have a situation — what do I grab?*

For install commands and the full tool list see the [README](../README.md).
For the version aimed at the agent itself (setup, tool map, house rules), see
[`skills/ironharness/SKILL.md`](../skills/ironharness/SKILL.md).

## The problem it solves

An LLM agent driving real hardware today usually does it with raw pyserial in
a bash loop. That works right up until it doesn't, and the failures are all
the same:

- the agent opens a port and blocks on a read forever — no timeout, no
  deadline;
- output that arrived between two tool calls is simply gone;
- it flashes or erases a board and nothing records who did what, when, and
  what came back;
- a session that crashed leaves nothing behind: the next session starts from
  zero and re-derives everything by trial and error;
- "it worked" is unverifiable — the return code said 0.

ironharness is the hands-with-rules layer between the agent and the device:
an MCP server (32 tools) where every operation is journaled to JSONL,
deadline- and quota-bounded, destructive actions sit behind explicit opt-in
gates, and domain errors come back as readable text with a suggested next
step instead of a stack trace.

**Proof it matters, not just claims**: the [ops
demo](https://cezman.github.io/ironharness/ops-demo.html) performs the same
job twice — wipe an ESP32 and write a meteo station back — once with bare
esptool plus hand-rolled serial scripting, once through ironharness MCP
tools, with the journal as the audit trail.

## I've never done this — ten minutes, no hardware required

1. **Wire the server into any MCP client** (Claude Code, Claude Desktop,
   Cursor, Codex, OpenCode…):

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

2. **Ask the agent: "list my serial ports".** It calls `serial_list` and you
   get ports with their USB identity (VID/PID, COM number, description). No
   board needed — an empty list is a valid, honest answer.

3. **Ask the agent: "open loop://, write hello and read it back".** `loop://`
   is a virtual port built into pyserial: everything written comes straight
   back. This exercises open → write → read → close with zero hardware and
   zero risk.

4. **Now the point of the whole thing — look at the journal:**

   ```
   type %USERPROFILE%\.ironharness\journal.jsonl    :: Windows
   cat ~/.ironharness/journal.jsonl                 # Linux / macOS
   ```

   Every operation from steps 2–3 is in there: what was opened, what was
   written, what came back, what was refused. This JSONL file is the
   contract: an agent, a human, or tooling can reconstruct exactly what
   happened even after a crash. Operations without a journal entry are
   treated as never having happened.

5. **Got a board?** Plug it in and ask: *"which port is my CH340 board on?
   Open it and show me what it prints."* The agent matches the port by
   VID:PID (CH340 = `1a86:7523`), opens it (directly or via `by-serial:`),
   and waits out the boot banner before writing — because opening a USB-UART
   port can pulse the board into a reboot, and writing into a half-booted
   REPL eats commands.

6. **Optional, recommended:** drop
   [`skills/ironharness/SKILL.md`](../skills/ironharness/SKILL.md) into your
   agent's skills directory. It carries the tool map and the paid-for
   lessons: paste mode for multi-line MicroPython code, deadlines are not
   yours to disable, the journal is the agent's memory.

See it in motion: the [demo GIF](demo.gif) of the wipe-and-rewrite scenario
on a live board, and [the board itself](board.jpg).

## I know what I'm doing — the map

`io-core` (shipped as the PyPI package `ironharness`; the MCP server) is one layer of small modules:

| Module | What it gives you |
|---|---|
| `session.py` | `Session` — named transports + file sandbox + one journal; what the MCP tools wrap. `session_status` is the self-check call after any context loss. |
| `serial_transport.py`, `serial_reader.py`, `mprepl.py` | pyserial surface (local ports only, network URLs refused), a background reader (device output between tool calls is buffered, not lost), file transfer to/from a MicroPython board over the raw REPL. |
| `modbus_transport.py`, `modbus_sim.py` | Modbus TCP client (FC3/6/16) and an in-process simulator for offline work. |
| `mqtt_transport.py`, `mqtt_sim.py` | MQTT 3.1.1 client (plaintext TCP — documented, bench-only) and a minimal in-process broker. |
| `esp_flash.py` | esptool wrapper: offline firmware-image parsing, flash/erase behind `IRONHARNESS_ALLOW_REAL_FLASH=1`. |
| `file_sandbox.py` | file transport confined to its root, with quotas. |
| `journal.py` | JSONL journal: size-based rotation, sidecar lock — safe with several processes writing. |
| `replay.py` | replayer: rebuild a session from its journal. |
| `limits.py` | `DeadlineTransport`, `RateLimitedTransport` — deadlines and quotas around any transport. |
| `verify.py` | `expect_read`, `write_and_expect` — effect verification instead of return-code hope. |
| `policy.py` | operator-controlled allowlists: `IRONHARNESS_ALLOWED_HOSTS`, `IRONHARNESS_ENABLED_KINDS`; every denial is journaled. |
| `faults.py` | `FaultyTransport` — scripted disconnects, delays, bit corruption, byte loss over any transport. |
| `errors.py` | domain error types; the agent receives them as readable text with the next step — a timeout says to close and reopen the port, a refused open points at the port whitelist. |
| `mcp_server.py` | the 32 tools over stdio. |

The safety model in five lines:

1. **Everything is journaled** — an operation without a journal entry didn't
   happen.
2. **Files live only inside the sandbox**, quotas on everything.
3. **Deadlines and byte caps everywhere** — a hung device cannot hang the
   agent, and a flood cannot blow up its context.
4. **Destructive means gated**: flash/erase need the `[flash]` extra plus an
   opt-in env var; live-board bench runs need `IRONBENCH_REAL_PORT` and
   `--allow-real`, and the board's `main.py` is backed up before any wipe.
5. **The network is operator-controlled**: serial is whitelisted to local
   ports, Modbus/MQTT destinations can be locked to a host allowlist.

Extending it: a new transport is "implement the primitive, wrap in deadline +
rate limit + policy, journal every operation, expose via the server".
Benchmark tasks are plain YAML in `src/ironbench/tasks/` — a task declares
its firmware, its detector (what observable behavior counts as PASS), and
optionally expert notes.

## Recipes: I have this situation — what do I grab?

| Situation | Grab | Where |
|---|---|---|
| Several USB-UART adapters, port numbers drift across re-plugs | `serial_list` → match VID:PID; `serial_wait` blocks until your board appears; open via `by-serial:<sn>` | MCP tools |
| Device prints between tool calls and the output is lost | background reader: `serial_reader_start`, then `serial_tail` / `serial_read_until` | MCP tools |
| Board wedged, want a clean attempt | `serial_reset` (RTS pulse) | MCP tools |
| Put a file on the board / rescue its `main.py` | `serial_put` / `serial_get` (raw REPL; back up before overwriting) | MCP tools |
| Flash or erase an ESP32 | `esp_image_info` (offline parse) → `esp_flash` / `esp_erase`, refused without `IRONHARNESS_ALLOW_REAL_FLASH=1` | MCP tools + `ironharness[flash]` |
| Need a fake device to develop against | `ModbusSimServer`, `python -m io_core.mqtt_sim` (source clone), `loop://` port | `src/io_core/modbus_sim.py`, `src/io_core/mqtt_sim.py` |
| Need an unreliable line for resilience tests | `FaultyTransport`: scripted disconnects, delays, corruption | `src/io_core/faults.py` |
| Talk Modbus or MQTT | `modbus_open/read/write/close`, `mqtt_open/publish/subscribe/read/close`; lock destinations with `IRONHARNESS_ALLOWED_HOSTS` | MCP tools |
| "What exactly did the agent do?" | read the journal; replay the session | `src/io_core/journal.py`, `src/io_core/replay.py`, `src/ironbench/journal_view.py` |
| The agent must *prove* an effect happened | `expect_read`, `write_and_expect` | `src/io_core/verify.py` |
| The agent is too enthusiastic (runaway writes, endless opens) | deadlines and caps are already around every transport; switch whole transport kinds off with `IRONHARNESS_ENABLED_KINDS` | `src/io_core/limits.py`, `src/io_core/policy.py` |
| A sensor on the I2C bus — is it even alive? | reference pattern: the `bus-diagnose` golden task (scan the bus, report what's missing, degrade without crashing) | `src/ironbench/tasks/` |
| Read a sensor with no libraries on the board | `bme-read` (BME280 straight from the datasheet), `cross-sensor` (BME280 + DS18B20, cross-checked) | `src/ironbench/tasks/` |
| Debug deployed firmware that misbehaves | the `debug-*` task class: the bug ships inside the task, PASS = the fix satisfies the spec | `src/ironbench/tasks/` |
| Measure what a model can actually do on firmware | `ironbench solve` → `ironbench report` (pass@k, per-class profiles) | README → ironbench |

## The benchmark half (ironbench)

If your goal is measuring agents rather than driving hardware: ironbench
ships a catalog of golden firmware tasks (`python -m ironbench.cli list`),
each with a class (io / data / protocol / fsm / control / resilience / debug)
and a level from 1 to 5, runnable on five targets — Wokwi ESP32/MicroPython,
the MicroPython unix port, Renode, a closed-loop plant simulation, and a live
board. A task counts as solved only when the *runner* verifies the firmware's
observable behavior — serial protocol responses, pin timing, sensor values —
never "the strings looked right". Numbers, archived generations with raw
per-attempt data, and the case studies live on
[gh-pages](https://cezman.github.io/ironharness/).

## Where everything lives

- [README](../README.md) — install, the full tool list, safety notes.
- [`skills/ironharness/SKILL.md`](../skills/ironharness/SKILL.md) — the same
  facts, written for the agent.
- [Ops demo](https://cezman.github.io/ironharness/ops-demo.html) — the "why",
  demonstrated on a live board.
- [Case study: expert notes](https://cezman.github.io/ironharness/case-study-notes-ab.html)
  — 3 lines of notes vs a full 5-iteration budget.
- [Leaderboard](https://cezman.github.io/ironharness/) ·
  [generations](https://cezman.github.io/ironharness/generations/).
- [demo.gif](demo.gif), [board.jpg](board.jpg) — the hardware, in the flesh.
