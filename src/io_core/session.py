"""Agent session: named transports + sandbox + unified journal.

The unit of agent work: MCP tools operate on a session, and every operation
lands in the session journal automatically (the "no log = didn't happen"
convention).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import serial.tools.list_ports

from io_core import mprepl
from io_core.errors import PolicyViolation
from io_core.file_sandbox import FileSandbox
from io_core.journal import JsonlJournal
from io_core.limits import (
    TRANSPORT_DEADLINE_ENV,
    TRANSPORT_RATE_ENV,
    DeadlineTransport,
    RateLimitedTransport,
    RateLimiter,
    parse_transport_deadline,
    parse_transport_rate,
)
from io_core.modbus_transport import ModbusTransport
from io_core.mqtt_transport import MqttTransport
from io_core.policy import MAX_CONNECTIONS_ENV, AccessPolicy, parse_max_connections
from io_core.serial_reader import SerialReader
from io_core.serial_transport import SerialTransport

DEFAULT_BOOTLOADER_OFFSET = 0x1000  # classic ESP32 (canonical value lives in esp_flash)

EventHook = Callable[[str, dict[str, Any]], None]


class Session:
    """Agent session: named transports + sandbox + unified journal.

    Thread safety (IH-12): Session methods may be called from several threads
    (MCP servers dispatch tool calls concurrently; MQTT emits come from the
    paho network thread). The transport registry is guarded by an internal
    lock, and open is check-and-insert under the same lock - a duplicate or
    over-the-limit open can never slip through a check-then-act window.
    The lock covers the registry and open/close paths, not per-transport
    operations: two threads driving one connection are not serialized
    (transports are not per-operation thread-safe; such misuse fails loudly
    on the transport level, it does not corrupt the registry).
    """

    def __init__(
        self,
        journal_path: str | Path,
        sandbox_root: str | Path,
        *,
        actor: str = "agent",
    ) -> None:
        self.journal = JsonlJournal(journal_path, actor=actor)
        self.sandbox = FileSandbox(sandbox_root, on_event=self.journal)
        self._transports: dict[str, Any] = {}
        self._kinds: dict[str, str] = {}
        # raw (unwrapped) serial transports and their background readers; the
        # reader must bypass the deadline/rate wrappers (a background pump is
        # not an agent operation) and the write path needs the raw handle's
        # owner to serialize against it (CH340 single-threaded link)
        self._serial_base: dict[str, Any] = {}
        self._readers: dict[str, SerialReader] = {}
        self._closed = False
        self._lock = threading.RLock()

    def _check_open(self) -> None:
        """Operations on a closed session raise a typed error (IH-32): the old
        behaviour resurrected transports (a fresh open on a closed session
        leaked past close() and was never cleaned up)."""
        if self._closed:
            from io_core.errors import TransportClosedError

            raise TransportClosedError("session is closed - create a new Session")

    def _check_free(self, name: str) -> None:
        if name in self._transports:
            raise KeyError(f"transport {name!r} is already open")

    def _check_connection_limit(self) -> None:
        """Connection-count policy; called under the session lock together with
        the registry insert, so parallel opens cannot slip past the ceiling."""
        limit = parse_max_connections(os.environ.get(MAX_CONNECTIONS_ENV))
        if limit is not None and len(self._transports) >= limit:
            self.journal(
                "policy_violation",
                {"rule": "max_connections", "limit": limit, "open": len(self._transports)},
            )
            raise PolicyViolation(
                f"connection limit reached "
                f"({len(self._transports)}/{limit} via {MAX_CONNECTIONS_ENV})"
            )

    def _check_kind(self, kind: str) -> None:
        AccessPolicy.from_env(on_event=self.journal).check_kind(kind)

    def _get(self, name: str) -> Any:
        with self._lock:
            try:
                return self._transports[name]
            except KeyError:
                raise KeyError(f"transport {name!r} is not open") from None

    def _journal_for(self, name: str) -> EventHook:
        """Per-connection journal hook: stamps every transport event with the
        connection name, so a session with two devices stays attributable -
        without it a read/write event carries only data and the journal cannot
        say whose bytes they are. The transport keeps owning its own fields
        (open/close events already carry port/host); conn is stamped LAST
        (IH-32), so a payload key "conn" can never shadow the attribution."""
        def hook(kind: str, data: dict[str, Any]) -> None:
            self.journal(kind, {**data, "conn": name})

        return hook

    def _apply_limits(self, t: Any, name: str) -> Any:
        """Wraps a fresh transport in the configured deadline/rate limits -
        the 'transports always have timeouts and quotas' convention for the
        standard session (IH-17). Deadline is on by default (600 s,
        IRONHARNESS_TRANSPORT_DEADLINE to reconfigure or 0/off to disable);
        the rate limiter only when IRONHARNESS_TRANSPORT_RATE=max/window is
        set. Env is re-read per open, like the access policy. The deadline is
        an IDLE window (sliding, IH-35): every successful operation pushes it
        out, so a long-lived interactive session survives; a connection with
        no successful operation for the window gets OperationTimeout with the
        conn name and a reopen hint, and must be re-opened. Limit denials are
        journaled (deadline_denied / rate_denied, IH-32) with the conn name
        (IH-35: the closure stamps the name regardless of registry state)."""
        deadline = parse_transport_deadline(os.environ.get(TRANSPORT_DEADLINE_ENV))
        if deadline is not None:
            t = DeadlineTransport(
                t, deadline, on_event=self._journal_for(name), label=name
            )
        rate = parse_transport_rate(os.environ.get(TRANSPORT_RATE_ENV))
        if rate is not None:
            max_calls, per_seconds = rate
            t = RateLimitedTransport(t, RateLimiter(max_calls, per_seconds),
                                     on_event=self._journal_for(name))
        return t

    # --- serial ---

    def serial_list(self) -> list[dict[str, Any]]:
        """Enumerates host serial ports with USB identity (device, vid, pid,
        description) - the MCP-first replacement for out-of-band pyserial
        diagnostics (IH-36): board COM numbers float across re-plugs, so
        agents must re-enumerate before serial_open. No port is opened and
        nothing is consumed (read-only over the host device table); the
        enumeration is journaled like every operation."""
        self._check_open()
        self._check_kind("serial")
        ports = [
            {
                "device": p.device,
                "vid": f"{p.vid:04x}" if p.vid is not None else None,
                "pid": f"{p.pid:04x}" if p.pid is not None else None,
                "description": p.description,
            }
            for p in serial.tools.list_ports.comports()
        ]
        self.journal("serial_listed", {"count": len(ports), "ports": ports})
        return ports

    def serial_open(
        self, name: str, port: str, *, baudrate: int = 115200, timeout: float = 1.0
    ) -> None:
        self._check_open()
        # check + open + insert under one lock: two parallel opens of one name
        # used to both pass the free-check, open two real ports and lose one of
        # them (it stayed open past session.close() - a leaked COM port)
        with self._lock:
            self._check_kind("serial")
            self._check_free(name)
            self._check_connection_limit()
            raw = SerialTransport(port, baudrate=baudrate, timeout=timeout,
                                  on_event=self._journal_for(name))
            t = self._apply_limits(raw, name)
            t.open()
            self._transports[name] = t
            self._kinds[name] = "serial"
            self._serial_base[name] = raw

    def serial_write(self, name: str, data_hex: str) -> int:
        try:
            data = bytes.fromhex(data_hex)
        except ValueError as e:
            # journal before raising (IH-34): a bad argument used to surface as
            # an anonymous tool error with no trace in the journal
            self.journal(
                "write_failed",
                {"conn": name, "error": f"data_hex is not valid hex: {e}"},
            )
            raise ValueError(f"data_hex is not valid hex: {e}") from e
        # lock order: session lock is ALWAYS released before io_lock is taken.
        # The inverse order (io_lock held while waiting for the session lock,
        # e.g. in _get) plus a concurrent reader_stop (session lock held,
        # joining a reader thread that waits for io_lock) is a deadlock cycle.
        with self._lock:
            t = self._get(name)
            reader = self._readers.get(name)
        if reader is not None:
            # CH340: serialize against the background read (racing them on a
            # live link bursts NUL bytes); limits still apply - the wrapper's
            # write goes under the reader's I/O lock
            with reader.io_lock:
                return t.write(data)
        return t.write(data)

    def serial_read(self, name: str, size: int = 64) -> str:
        with self._lock:
            if name in self._readers:
                raise RuntimeError(
                    f"transport {name!r} has a background reader - use serial_tail/"
                    "serial_read_until (a direct read would race the reader for bytes)"
                )
            t = self._get(name)
        return t.read(size).hex()

    def serial_read_line(self, name: str, max_len: int = 256) -> str:
        with self._lock:
            if name in self._readers:
                raise RuntimeError(
                    f"transport {name!r} has a background reader - use serial_tail/"
                    "serial_read_until (a direct read would race the reader for bytes)"
                )
            t = self._get(name)
        return t.read_line(max_len).hex()

    def serial_reset(self, name: str, *, pulse_sec: float = 0.1, settle_sec: float = 2.0) -> None:
        """Resets the board with an RTS pulse (ESP32: RTS->EN, DTR stays low
        = normal boot) - a clean state between solve attempts or after a hung
        REPL. open() never resets (the idle-lines fix); reset is explicit.
        Under a reader's I/O lock: line control is I/O on the same single
        line (CH340). The settle wait holds the lock too - writes and drains
        are blocked while the board boots; boot output accumulates in the
        driver RX buffer and reaches the reader after the wait (a very
        chatty boot can overflow a small driver buffer - keep settle_sec
        realistic)."""
        with self._lock:
            t = self._get(name)
            reader = self._readers.get(name)
        if reader is not None:
            with reader.io_lock:
                t.reset(pulse_sec=pulse_sec, settle_sec=settle_sec)
        else:
            t.reset(pulse_sec=pulse_sec, settle_sec=settle_sec)

    def serial_put(self, name: str, source_path: str, target_path: str) -> int:
        """Pushes a sandbox file onto the device filesystem over the raw REPL
        (IH-34): target_path is overwritten. source_path is sandbox-relative -
        the agent stages content with file_write, the transfer never touches
        host paths. Refuses under an active background reader: the transfer
        reads the board's answers, a reader would steal those bytes. The
        whole exchange is journaled; a failed transfer lands as
        serial_put_failed with the board's error text.
        """
        self._check_open()
        self._check_kind("serial")
        try:
            data = self.sandbox.read_file(source_path)
        except Exception as e:
            self.journal(
                "serial_put_failed",
                {"conn": name, "source": source_path, "target": target_path, "error": str(e)},
            )
            raise
        with self._lock:
            if name in self._readers:
                raise RuntimeError(
                    f"transport {name!r} has a background reader - stop it before "
                    "serial_put (the transfer reads the board's answers)"
                )
            t = self._get(name)
        try:
            mprepl.put_file(t, data, target_path)
        except Exception as e:
            self.journal(
                "serial_put_failed",
                {"conn": name, "source": source_path, "target": target_path, "error": str(e)},
            )
            raise
        self.journal(
            "serial_put",
            {"conn": name, "source": source_path, "target": target_path, "bytes": len(data)},
        )
        return len(data)

    def serial_get(self, name: str, target_path: str, dest_path: str) -> int:
        """Pulls a device file into the sandbox over the raw REPL (IH-34):
        target_path on the board, dest_path sandbox-relative (overwritten,
        sandbox quotas apply). Same reader and journaling contract as
        serial_put; a missing board file lands as serial_get_failed carrying
        the board's ENOENT traceback.
        """
        self._check_open()
        self._check_kind("serial")
        with self._lock:
            if name in self._readers:
                raise RuntimeError(
                    f"transport {name!r} has a background reader - stop it before "
                    "serial_get (the transfer reads the board's answers)"
                )
            t = self._get(name)
        try:
            data = mprepl.get_file(t, target_path)
        except Exception as e:
            self.journal(
                "serial_get_failed",
                {"conn": name, "source": target_path, "target": dest_path, "error": str(e)},
            )
            raise
        try:
            written = self.sandbox.write_file(dest_path, data, overwrite=True)
        except Exception as e:
            self.journal(
                "serial_get_failed",
                {"conn": name, "source": target_path, "target": dest_path, "error": str(e)},
            )
            raise
        self.journal(
            "serial_get",
            {"conn": name, "source": target_path, "target": dest_path, "bytes": written},
        )
        return written

    # --- serial background reader (IH-18) ---

    def serial_reader_start(self, name: str, *, max_bytes: int = 65536) -> dict[str, int]:
        """Starts a background reader on an open serial transport: the reader
        drains the port into a bounded buffer, so device output between agent
        operations is not lost. The reader owns the read side: serial_read/
        serial_read_line refuse while it runs (serial_write is serialized
        against it - CH340 single-threaded link)."""
        with self._lock:
            base = self._serial_base.get(name)
            if base is None:
                raise KeyError(f"serial transport {name!r} is not open")
            if name in self._readers:
                raise KeyError(f"transport {name!r} already has a background reader")
            reader = SerialReader(base, max_bytes=max_bytes)
            self._readers[name] = reader
            # journaled BEFORE the thread starts: a journal where the first
            # background read precedes reader_start would be a lie about order
            self.journal("reader_start", {"conn": name, "max_bytes": max_bytes})
            reader.start()
        return reader.stats()

    def serial_reader_stop(self, name: str) -> dict[str, int]:
        with self._lock:
            reader = self._readers.pop(name, None)
            if reader is None:
                raise KeyError(f"transport {name!r} has no background reader")
            reader.stop()
            stats = reader.stats()
        self.journal("reader_stop", {"conn": name, **stats})
        return stats

    def serial_tail(self, name: str, size: int = 4096) -> dict[str, object]:
        """Newest `size` bytes from the reader buffer (non-destructive)."""
        return self._reader(name).tail(size)

    def serial_read_until(self, name: str, pattern: str, timeout: float = 10.0) -> dict[str, object]:
        """Waits for the utf-8 pattern in fresh reader data; consumes the
        buffer up to the end of the match (expect-style)."""
        return self._reader(name).read_until(pattern, timeout)

    def _reader(self, name: str) -> SerialReader:
        with self._lock:
            try:
                return self._readers[name]
            except KeyError:
                raise KeyError(f"transport {name!r} has no background reader") from None

    def _stop_reader(self, name: str, *, implicit: bool = False) -> None:
        """Stops and drops the reader if one is attached (under the session
        lock). An implicit stop (transport close) is journaled too: the
        journal must show the reader ending, not vanishing."""
        reader = self._readers.pop(name, None)
        if reader is not None:
            reader.stop()
            if implicit:
                self.journal("reader_stop", {"conn": name, "implicit": True, **reader.stats()})

    # --- modbus ---

    def modbus_open(
        self,
        name: str,
        host: str,
        *,
        port: int = 502,
        device_id: int = 1,
        timeout: float = 3.0,
    ) -> None:
        self._check_open()
        with self._lock:
            self._check_kind("modbus")
            self._check_free(name)
            self._check_connection_limit()
            t = self._apply_limits(
                ModbusTransport(
                    host,
                    port=port,
                    device_id=device_id,
                    timeout=timeout,
                    on_event=self._journal_for(name),
                ),
                name,
            )
            t.open()
            self._transports[name] = t
            self._kinds[name] = "modbus"

    def modbus_read(self, name: str, address: int, count: int = 1) -> list[int]:
        return self._get(name).read_holding(address, count)

    def modbus_write(self, name: str, address: int, values: list[int]) -> None:
        t = self._get(name)
        if len(values) == 1:
            t.write_register(address, values[0])
        else:
            t.write_registers(address, values)

    # --- mqtt ---

    def mqtt_open(
        self,
        name: str,
        host: str,
        *,
        port: int = 1883,
        client_id: str = "",
        timeout: float = 3.0,
    ) -> None:
        self._check_open()
        with self._lock:
            self._check_kind("mqtt")
            self._check_free(name)
            self._check_connection_limit()
            t = self._apply_limits(
                MqttTransport(
                    host,
                    port=port,
                    client_id=client_id,
                    timeout=timeout,
                    on_event=self._journal_for(name),
                ),
                name,
            )
            t.open()
            self._transports[name] = t
            self._kinds[name] = "mqtt"

    def mqtt_publish(self, name: str, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> None:
        self._get(name).publish(topic, payload, qos=qos, retain=retain)

    def mqtt_subscribe(self, name: str, topic: str, *, qos: int = 0) -> None:
        self._get(name).subscribe(topic, qos=qos)

    def mqtt_read(self, name: str, timeout: float = 1.0) -> dict[str, str] | None:
        return self._get(name).read_message(timeout)

    # --- esp (flashing via esptool; needs the [flash] extra) ---

    def esp_image_info(self, firmware_path: str, chip: str = "esp32") -> dict[str, Any]:
        self._check_open()
        self._check_kind("esp")
        from io_core.esp_flash import EspFlasher  # lazy: esptool is an optional dependency

        return EspFlasher(chip=chip, on_event=self.journal).image_info(firmware_path)

    def esp_flash(
        self, port: str, firmware_path: str, *, addr: int = DEFAULT_BOOTLOADER_OFFSET, baud: int = 921600
    ) -> str:
        self._check_open()
        self._check_kind("esp")
        from io_core.esp_flash import EspFlasher  # lazy: esptool is an optional dependency

        return EspFlasher(on_event=self.journal).flash(port, firmware_path, addr=addr, baud=baud)

    def esp_erase(self, port: str, *, baud: int = 921600) -> str:
        self._check_open()
        self._check_kind("esp")
        from io_core.esp_flash import EspFlasher  # lazy: esptool is an optional dependency

        return EspFlasher(on_event=self.journal).erase(port, baud=baud)

    # --- файлы (песочница) ---

    def file_write(self, path: str, content: str) -> int:
        self._check_open()
        self._check_kind("file")
        return self.sandbox.write_file(path, content.encode("utf-8"), overwrite=True)

    def file_read(self, path: str) -> str:
        self._check_open()
        self._check_kind("file")
        return self.sandbox.read_file(path).decode("utf-8", errors="replace")

    def file_list(self, path: str = ".") -> list[str]:
        self._check_open()
        self._check_kind("file")
        return self.sandbox.list_dir(path)

    def file_delete(self, path: str) -> None:
        self._check_open()
        self._check_kind("file")
        self.sandbox.delete_file(path)

    # --- жизненный цикл ---

    def _close_typed(self, name: str, cls: type, kind: str) -> None:
        # the name is freed before close(): a failing close must not leave a
        # half-closed transport occupying the name. The kind is tracked in a
        # registry (not isinstance): since IH-17 the registry holds limit
        # wrappers, not bare transports
        with self._lock:
            t = self._get(name)
            if self._kinds.get(name) != kind:
                raise KeyError(f"transport {name!r} is not a {kind} transport")
            self._stop_reader(name, implicit=True)  # a reader must not outlive its transport
            del self._transports[name]
            del self._kinds[name]
            self._serial_base.pop(name, None)
            t.close()

    def serial_close(self, name: str) -> None:
        self._close_typed(name, SerialTransport, "serial")

    def modbus_close(self, name: str) -> None:
        self._close_typed(name, ModbusTransport, "modbus")

    def mqtt_close(self, name: str) -> None:
        self._close_typed(name, MqttTransport, "mqtt")

    def close_transport(self, name: str) -> None:
        with self._lock:
            t = self._get(name)  # a friendly "not open" error, not a bare KeyError
            self._stop_reader(name, implicit=True)  # a reader must not outlive its transport
            del self._transports[name]
            del self._kinds[name]  # keep the two registries in lockstep
            self._serial_base.pop(name, None)
        t.close()

    def close(self) -> None:
        """Closes all transports and the journal. A failing transport close is
        collected and re-raised only after everything else (including the
        journal) has been closed - one stuck port must not leak the rest of the
        session."""
        self._closed = True
        first_error: BaseException | None = None
        with self._lock:
            transports = list(self._transports.values())
            self._transports.clear()
            self._kinds.clear()
            self._serial_base.clear()
            readers = list(self._readers.items())
            self._readers.clear()
        for name, reader in readers:  # stop readers before their transports close
            try:
                reader.stop()
                # an implicit stop must end in the journal like any other
                # (a reader that merely vanishes breaks the audit trail)
                self.journal("reader_stop", {"conn": name, "implicit": True, **reader.stats()})
            except Exception as e:  # noqa: BLE001 - one bad reader must not leak the rest
                if first_error is None:
                    first_error = e
        for t in transports:
            try:
                t.close()
            except Exception as e:  # noqa: BLE001 - one bad port must not leak the rest
                if first_error is None:
                    first_error = e
        try:
            self.journal.close()
        except (OSError, ValueError) as e:
            if first_error is None:
                first_error = e
        if first_error is not None:
            raise first_error
