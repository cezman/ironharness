# Sim-vs-real smoke: uart-echo (2026-09-08)

First run of the same golden task on a simulator and on live hardware
(stage 3, IH-2/IH-3). This is a harness smoke with the reference solution —
no LLM agent yet; the pass@k sim-vs-real campaign waits for a local LLM
endpoint.

| target | command | result | time |
|--------|---------|--------|------|
| unix (MicroPython unix-port, WSL2) | `ironbench run --task uart-echo --target unix` | **PASS** | 7.9 s |
| real (ESP32 Wroom-32 + CH340, COM4, MicroPython v1.27) | `IRONBENCH_REAL_PORT=COM4 ironbench run --task uart-echo --target real` | **PASS** | 20.9 s |

## What this confirms

- The same entry (`uart-echo/solution.py`: banner + infinite input()-echo
  loop) passes the `task.yaml` criteria on both targets with zero code
  changes.
- Real-target overhead: ~13 s over the simulator — line-by-line staging,
  boot pauses after the port-open reset, soft reset (main.py hygiene).

## CH340/REPL gotchas closed in realhw.py (matters for future real targets)

1. Concurrent read+write from two threads crashes the CH340 driver
   (segfault) — the link is single-threaded: pump-reads only between steps,
   writes rate-limited (24 bytes / 40 ms).
2. Staging the script in one burst corrupts it (the board receives NUL
   bytes) — send line-by-line, 24 B chunks with delays.
3. `input()` terminates on `\r` only (`\n` is silent) — stimulus writes are
   normalized to `\r` (mirroring the unix target, which needs `\n`).
4. Paste mode is confirmed by its banner; before a run, a foreign `main.py`
   is removed and a soft reset is performed; the output buffer is cleared
   after staging — pattern scoring only sees the run itself.

## Next step

`ironbench solve --task uart-echo --target {unix,real} --attempts N` against
a local LLM (`LLM_BASE_URL`/`LLM_MODEL`) → pass@k on simulator vs hardware —
the sim-vs-real campaign for the announcement.

## First LLM datapoint (2026-09-09)

Model: `qwen2.5-7b-instruct` (LM Studio, local, 4.7 GB, fully in VRAM).
Task: uart-echo, 2 attempts, iteration limit 5 (harness defaults).

| target | attempts | solved | wall time |
|--------|----------|--------|-----------|
| unix (sim) | 2 | **0/2** | 58.8 s |
| real (ESP32, COM4) | 2 | **0/2** | 648.9 s |

Failure mode (both targets): the model fixates on hardware-UART solutions
(`machine.UART(0/1)` + blocking reads), while the harness channel is
stdin/stdout — `input()`, which the reference solution uses. On unix,
`machine.UART` does not exist (AttributeError on every iteration); on real,
UART1 has no physical peer, so the echoed lines never come back.

Caveats: task descriptions are still Russian (IH-9 translation pending);
5 iterations is the harness default, not a tuned budget.

Next: rerun with `qwen3.5-9b` (reasoning model, slower — the first try hit a
300 s per-call timeout; needs `LLM_TIMEOUT=900+`), then extend to more tasks.
