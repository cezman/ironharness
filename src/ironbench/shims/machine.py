"""Harness-provided `machine` shim for the unix target (IH-16).

The MicroPython unix port has no `machine` module; this shim lets GPIO-style
firmware run there. Pin state changes are logged to stdout as
`PIN <id> <value> <t_ms>` - one line per explicit set, timestamped with the
monotonic clock in milliseconds. The runner's `events:` scoring parses these
lines: periodicity and count of the transitions become verifiable criteria
(a bare print of the expected strings does not produce well-timed events).

This is a model, not a GPIO peripheral: the "hardware" state lives in the
shim instance only. Runs under both MicroPython (WSL) and CPython (tests):
no __future__ import - the MicroPython unix port has no __future__ module.
"""

import sys
import time


def _now_ms() -> float:
    if hasattr(time, "ticks_ms"):
        return float(time.ticks_ms())
    return time.monotonic() * 1000.0


class Pin:
    IN = 1
    OUT = 2
    PULL_UP = 1
    PULL_DOWN = 2
    IRQ_RISING = 1
    IRQ_FALLING = 2

    def __init__(self, id, mode=IN, value=None, pull=None, *, alt=None):
        self._id = id
        self._mode = mode
        self._value = 0 if value is None else int(bool(value))

    def _log(self) -> None:
        # write+flush instead of print(flush=True): the MicroPython print()
        # rejects keyword arguments
        sys.stdout.write(f"PIN {self._id} {self._value} {_now_ms():.1f}\n")
        sys.stdout.flush()

    def value(self, v=None):
        if v is None:
            return self._value
        new = int(bool(v))
        if new != self._value:
            self._value = new
            self._log()
        return self._value

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def toggle(self):
        self.value(0 if self._value else 1)

    def low(self):
        self.off()

    def high(self):
        self.on()
