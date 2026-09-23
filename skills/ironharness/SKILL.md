---
name: ironharness
description: Safe hardware I/O for agents via the ironharness MCP server - serial, Modbus TCP, MQTT, a file sandbox, ESP32 flash tools. Setup, the tool map, and house rules for working with real hardware without hanging ports or bricking boards.
---

# ironharness — give your agent safe hands for hardware

Your agent already knows how to think and write code. What it lacks is hands for
the physical world: opening a serial port without hanging, talking Modbus,
flashing an ESP32 without bricking it, and leaving an audit trail. The
`ironharness` MCP server provides exactly that: every operation is journaled,
capped and deadline-bounded, and destructive actions are gated behind explicit
opt-in flags.

## Setup (one minute)

```bash
uvx --from ironharness ironharness-mcp        # runs on stdio, no install
```

Claude Code / any MCP client config:

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

Optional: `uvx --from 'ironharness[flash]' ironharness-mcp` adds esptool-based
`esp_flash`/`esp_erase` (they stay refused unless `IRONHARNESS_ALLOW_REAL_FLASH=1`
is set for the server process).

## The tool map (how to think about it)

- **Discover**: `session_status` (self-check after any context loss),
  `serial_list` (ports with VID/PID), `serial_wait` (block until your board
  appears), `echo` (connectivity check), `file_list` (the sandbox root).
- **Serial**: `serial_open` (accepts `by-serial:<sn>`; local ports only),
  `serial_write`, `serial_read`, `serial_read_line`, `serial_close`,
  `serial_put`, `serial_get`
  (files to/from the board over the raw REPL), `serial_reader_start`,
  `serial_reader_stop`, `serial_tail`, `serial_read_until` (a background
  reader: device output between your tool calls is buffered, not lost),
  `serial_monitor` (a bounded capture window: byte/time caps, quiet window,
  stop pattern, sandbox dump), `serial_reset` (RTS-pulse reset between
  attempts).
- **Buses and networks**: `modbus_open`, `modbus_read`, `modbus_write`,
  `modbus_close`, `mqtt_open`, `mqtt_publish`, `mqtt_subscribe`, `mqtt_read`,
  `mqtt_close` (destinations are operator-controlled;
  set `IRONHARNESS_ALLOWED_HOSTS` to lock them down).
- **Flash (opt-in)**: `esp_image_info` (offline image parse), `esp_flash`,
  `esp_erase` — refused without `IRONHARNESS_ALLOW_REAL_FLASH=1`. Erase is
  irreversible; always have a firmware image (and the board's `main.py`) backed
  up first.
- **Files**: `file_write`, `file_read`, `file_list`, `file_delete` — sandboxed and quota-bounded.

## House rules

1. **Discover before opening.** `serial_list` first; ports drift across
   re-plugs — match by VID:PID (CH340 = `1a86:7523`) or use `by-serial:`.
2. **Sim before real.** Try logic against `loop://`, simulators, or the file
   sandbox before touching hardware.
3. **The journal is your memory.** Every operation lands in
   `$IRONHARNESS_HOME/journal.jsonl` (default `~/.ironharness/`). After any
   confusion — call `session_status`, re-read the journal; do not guess.
4. **Errors teach.** Domain errors arrive as readable text with the next step
   (`deadline_denied` → reopen the connection; `serial_open_failed` → the port
   whitelist). Read them; never retry blind.
5. **Destructive = gated.** `esp_flash`/`esp_erase` need the `flash` extra and
   `IRONHARNESS_ALLOW_REAL_FLASH=1`. If a gate refuses you, that is the design,
   not a bug to route around.
6. **Deadlines are not yours to disable.** Transports are deadline- and
   quota-bounded by design; on a timeout, reopen instead of raising limits.
7. **MicroPython REPL gotchas** (paid lessons):
   - the cooked REPL auto-indents after any line ending with `:` — put
     multi-line blocks through paste mode (Ctrl+E … Ctrl+D) or write
     single-line expressions;
   - `input()` returns without the trailing `\n`; on dev boards the console is
     the USB REPL — `machine.UART` is usually NOT wired to it;
   - opening the port can pulse the board into a fresh boot (DTR/RTS lines) —
     wait for the boot banner before writing.

## MicroPython firmware mini-loop (the 20-second version)

`serial_open` → `serial_get` (back up the board's `main.py` first) →
`serial_put` (your main.py) → `serial_reset` → `serial_read_until("your boot
marker")` → iterate. Never leave the board without a working `main.py`.
