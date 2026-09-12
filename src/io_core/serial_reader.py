"""Background serial reader (IH-18): a thread that keeps draining a serial
transport into a bounded buffer, so output printed by the device between
agent operations (between solve iterations, while the agent thinks) is not
lost.

Why: a live board keeps printing while nobody calls serial_read - a
polling agent loses those bytes. The reader owns the READ side of the
transport; consumers use tail() (non-destructive look at the newest data)
and read_until(pattern, deadline) (expect-style, consumes the buffer up to
the end of the match).

CH340 constraint (single-threaded link): a read racing a write on a live
CH340 bursts NUL bytes (learned on hardware, see realhw.py). The reader
therefore holds an I/O lock around every background read, and the session
serializes writes through the same lock while a reader is attached - the
lock lives HERE, not in the transport, so the deadline/rate wrappers keep
working unchanged for agent operations.

Journaling stays exactly as before: the reader reads the RAW transport
(under the limit wrappers - a background pump is not an agent operation and
must not eat the rate budget), so every drained chunk is journaled as a
normal "read" event with the connection name and lands in the replay
stream like any other read.

The raw transport is read with its own timeout: while a background read is
inside its timeout window the I/O lock is held, so a concurrent write waits
up to that timeout (default 1 s). Open the transport with a small timeout
when low write latency matters.

Journal growth note: every drained chunk is journaled, and the deadline/
rate limits do NOT apply to the background pump - a chatty device at
115200 baud adds up to ~23 read events per second to the JSONL for as long
as the reader runs. The buffer in memory is bounded (max_bytes); the
journal is not - stop the reader when the session no longer needs the
stream.

Buffer model: a deque of ingested (stamp, chunk) pairs plus a byte offset
into the first chunk (what read_until has already consumed of it). The
bound is on live bytes; eviction drops whole oldest chunks and counts the
evicted bytes in `dropped`, so consumers can tell a partial history.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from io_core.errors import TransportClosedError

_POLL_IDLE_SEC = 0.05


class SerialReader:
    """Bounded drop-oldest buffer fed by a daemon thread.

    tail()/read_until() never block on the background thread - they take a
    snapshot under the same lock the thread appends with.
    """

    def __init__(self, transport: Any, *, max_bytes: int = 65536, chunk: int = 512) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be > 0, got {max_bytes}")
        if chunk <= 0:
            raise ValueError(f"chunk must be > 0, got {chunk}")
        if max_bytes < chunk:
            # every appended chunk would be evicted whole: the buffer could
            # never hold anything
            raise ValueError(f"max_bytes ({max_bytes}) must be >= chunk ({chunk})")
        self._transport = transport  # the RAW serial transport (no limit wrappers)
        self._max_bytes = max_bytes
        self._chunk = chunk
        self._chunks: deque[tuple[float, bytes]] = deque()
        self._bytes = 0  # live bytes in the buffer (including the consumed prefix)
        self._dropped = 0  # bytes evicted by the bound (the tail is a partial history)
        self._consumed = 0  # read_until's offset into the first chunk
        self._io_lock = threading.Lock()  # CH340: serializes background reads vs session writes
        self._lock = threading.Lock()  # guards the buffer (thread appends vs consumer snapshots)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    # --- lifecycle ---

    def start(self) -> None:
        """Starts the background thread; idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stops the thread and joins it. The transport is NOT closed here:
        the owner (Session) manages the transport lifetime."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            # No lock while WAITING for data: a lock held through the
            # transport's read timeout starves writers on slow runners (the
            # reader releases and re-acquires between reads and wins every
            # GIL convoy race - CI-proven by a faulthandler dump of two
            # writers parked on io_lock for a full dump window). The lock
            # guards only the actual drain, so the CH340 contract holds:
            # a read and a write never overlap - waiting for data is not an
            # I/O operation.
            try:
                in_waiting = getattr(self._transport, "in_waiting", None)
            except (OSError, ValueError):
                in_waiting = None  # dead/closed port: let read() below raise the real error
            data = b""
            if in_waiting:
                with self._io_lock:
                    if self._stop.is_set():
                        break
                    try:
                        data = self._transport.read(min(int(in_waiting), self._chunk))
                    except (OSError, ValueError, AssertionError, TransportClosedError) as e:
                        self._error = e
                        return  # port died: stop draining, surface on the next consumer call
            elif in_waiting == 0:
                time.sleep(_POLL_IDLE_SEC)  # nothing to drain - no lock taken at all
                continue
            else:
                # no in_waiting support (test fakes): one read attempt, then idle
                with self._io_lock:
                    if self._stop.is_set():
                        break
                    try:
                        data = self._transport.read(self._chunk)
                    except (OSError, ValueError, AssertionError, TransportClosedError) as e:
                        self._error = e
                        return
                if not data:
                    time.sleep(_POLL_IDLE_SEC)
            if data:
                with self._lock:
                    self._chunks.append((time.monotonic(), data))
                    self._bytes += len(data)
                    self._evict_locked()

    def _evict_locked(self) -> None:
        """Drops whole oldest chunks until the bound holds."""
        while self._bytes > self._max_bytes and self._chunks:
            _, oldest = self._chunks.popleft()
            live = len(oldest) - self._consumed
            self._dropped += max(0, live)
            self._bytes -= len(oldest)
            self._consumed = 0  # the first chunk (with the consumed prefix) is gone

    def error(self) -> BaseException | None:
        """The failure that killed the background thread, if any."""
        return self._error

    @property
    def io_lock(self) -> threading.Lock:
        """The lock serializing background reads against session writes
        (the CH340 single-threaded-link contract; Session.write takes it)."""
        return self._io_lock

    # --- buffer views ---

    def _snapshot_locked(self) -> bytes:
        """Live unconsumed buffer content (caller holds the lock)."""
        if not self._chunks:
            return b""
        first = True
        buf = bytearray()
        for _, chunk in self._chunks:
            if first:
                buf += chunk[self._consumed :]
                first = False
            else:
                buf += chunk
        return bytes(buf)

    def tail(self, size: int = 4096) -> dict[str, object]:
        """The newest `size` bytes of the buffer, non-destructive.

        Returns {data_hex, text, dropped, alive, error}: `dropped` is the
        number of bytes evicted by the bound plus bytes consumed by
        read_until - non-zero means the tail is a partial history, not
        everything the device sent. `alive`/`error` tell whether the
        background thread is still draining (a dead reader means the port
        failed - distinguish it from a silent device)."""
        with self._lock:
            buf = self._snapshot_locked()
            dropped = self._dropped + self._consumed if self._chunks else self._dropped
        return {
            "data_hex": buf[-size:].hex(),
            "text": buf[-size:].decode("utf-8", "replace"),
            "dropped": dropped,
            "alive": self.running,
            "error": str(self._error) if self._error is not None else None,
        }

    def read_until(self, pattern: str, timeout: float = 10.0) -> dict[str, object]:
        """Waits until the utf-8 `pattern` appears in fresh (unconsumed)
        buffer data, then consumes the buffer up to the end of the match.

        Returns {found, data_hex, text, alive, error}: on a match, everything
        from the current read position through the end of the pattern; on
        timeout (or when the reader thread died), whatever is unconsumed so
        far and found=False. Consumption makes consecutive read_until calls
        wait for NEW occurrences."""
        if not pattern:
            raise ValueError("pattern must not be empty")
        needle = pattern.encode("utf-8")
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                buf = self._snapshot_locked()
                pos = buf.find(needle)
                if pos >= 0:
                    end = pos + len(needle)
                    self._consume_locked(end)
                    return {
                        "found": True,
                        "data_hex": buf[:end].hex(),
                        "text": buf[:end].decode("utf-8", "replace"),
                        "alive": self.running,
                        "error": None,
                    }
            if self._error is not None or time.monotonic() >= deadline or self._stop.is_set():
                return {
                    "found": False,
                    "data_hex": buf.hex(),
                    "text": buf.decode("utf-8", "replace"),
                    "alive": self.running,
                    "error": str(self._error) if self._error is not None else None,
                }
            time.sleep(_POLL_IDLE_SEC)

    def _consume_locked(self, nbytes: int) -> None:
        """Removes the first nbytes of the unconsumed buffer (read_until took them)."""
        take = nbytes
        while take > 0 and self._chunks:
            avail = len(self._chunks[0][1]) - self._consumed
            if take < avail:
                self._consumed += take
                break
            _, chunk = self._chunks.popleft()
            self._bytes -= len(chunk)
            take -= avail
            self._consumed = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            live = self._bytes - (self._consumed if self._chunks else 0)
            return {
                "buffered": live,
                "dropped": self._dropped,
                "chunks": len(self._chunks),
            }
