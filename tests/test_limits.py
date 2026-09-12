"""Тесты лимитов и верификации (этап 1, задача 5). Часы — FakeClock, без пауз."""

import pytest

from io_core import (
    DeadlineTransport,
    OperationTimeout,
    RateLimitedTransport,
    RateLimiter,
    RateLimitExceeded,
    SerialTransport,
    VerificationError,
    expect_read,
    write_and_expect,
)
from io_core.limits import Clock


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def test_rate_limiter_fixed_window():
    clock: Clock = FakeClock()
    rl = RateLimiter(3, per_seconds=10, clock=clock)
    for _ in range(3):
        rl.acquire()
    with pytest.raises(RateLimitExceeded):
        rl.acquire()
    clock.advance(10)  # окно закрылось — счётчик обнулился
    rl.acquire()


def test_rate_limited_transport():
    clock: Clock = FakeClock()
    rl = RateLimiter(2, per_seconds=60, clock=clock)
    with RateLimitedTransport(SerialTransport("loop://", timeout=0.2), rl) as t:
        t.write(b"a")
        t.read(1)
        with pytest.raises(RateLimitExceeded):
            t.write(b"b")


def test_deadline_transport():
    clock: Clock = FakeClock()
    with DeadlineTransport(SerialTransport("loop://", timeout=0.2), seconds=5, clock=clock) as t:
        t.write(b"x")
        clock.advance(6)
        with pytest.raises(OperationTimeout):
            t.write(b"y")


def test_deadline_within_limit():
    clock: Clock = FakeClock()
    with DeadlineTransport(SerialTransport("loop://", timeout=0.2), seconds=5, clock=clock) as t:
        clock.advance(4.9)
        t.write(b"x")  # до дедлайна — работает
        t.read(1)


class _Boom:
    """A transport whose every operation fails (no I/O needed)."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def write(self, data):
        raise OSError("port gone")


def test_deadline_slides_on_successful_ops():
    # IH-35 pin: the deadline is an IDLE window, not a lifetime - every
    # successful operation pushes it out. Cumulative 8s with 4s gaps passes
    # on a 5s deadline (a lifetime deadline would condemn the second write),
    # then 6s of true silence denies. Fails on the pre-IH-35 semantics.
    clock: Clock = FakeClock()
    with DeadlineTransport(SerialTransport("loop://", timeout=0.2), seconds=5, clock=clock) as t:
        t.write(b"a")
        clock.advance(4)
        t.write(b"b")  # 4s idle: refreshes the window
        clock.advance(4)
        t.write(b"c")  # 8s since open, 4s idle: must pass
        clock.advance(6)
        with pytest.raises(OperationTimeout):
            t.write(b"d")  # 6s of silence: denied


def test_failed_operation_does_not_refresh_deadline():
    # IH-35: only successful operations slide the window - a transport that
    # keeps failing goes stale exactly as one that stays silent.
    clock: Clock = FakeClock()
    t = DeadlineTransport(_Boom(), seconds=5, clock=clock)
    t.open()
    with pytest.raises(OSError):
        t.write(b"x")  # fails: no refresh
    clock.advance(4)
    with pytest.raises(OSError):
        t.write(b"x")
    clock.advance(1.5)  # 5.5s since open, zero successful ops
    with pytest.raises(OperationTimeout):
        t.write(b"x")


def test_deadline_denial_message_names_connection_and_hint():
    # IH-35: the agent must learn WHICH connection went stale and WHAT to do.
    clock: Clock = FakeClock()
    events: list[tuple[str, dict]] = []
    t = DeadlineTransport(
        SerialTransport("loop://", timeout=0.2),
        seconds=5,
        clock=clock,
        on_event=lambda kind, data: events.append((kind, data)),
        label="esp",
    )
    t.open()
    clock.advance(6)
    with pytest.raises(OperationTimeout, match=r"'esp'.*close and reopen"):
        t.write(b"x")
    kind, data = events[-1]
    assert kind == "deadline_denied"
    assert data["deadline_sec"] == 5
    assert data["idle_sec"] >= 6  # the journal tells how long the line was silent


def test_reopen_resets_stale_deadline():
    # IH-35 negative path, paired: denied before the reopen, working after.
    clock: Clock = FakeClock()
    t = DeadlineTransport(SerialTransport("loop://", timeout=0.2), seconds=5, clock=clock)
    t.open()
    clock.advance(6)
    with pytest.raises(OperationTimeout):
        t.write(b"x")
    t.close()
    t.open()
    t.write(b"x")  # fresh window
    t.close()


def test_expect_read_echo():
    with SerialTransport("loop://", timeout=0.2) as t:
        t.write(b"hello")
        assert expect_read(t, b"hello", timeout=2) == b"hello"


def test_expect_read_timeout():
    with SerialTransport("loop://", timeout=0.05) as t, pytest.raises(VerificationError):
        expect_read(t, b"never-coming", timeout=0.3)


def test_write_and_expect_default_echo():
    with SerialTransport("loop://", timeout=0.5) as t:
        assert write_and_expect(t, b"abc", timeout=2) == b"abc"


def test_rate_limiter_is_thread_safe():
    # IH-12: N threads race acquire() against a limit of N-1 - exactly N-1
    # pass; without the lock the check-then-increment window lets all N through.
    import threading

    from io_core import RateLimiter
    from io_core.errors import RateLimitExceeded

    limiter = RateLimiter(max_calls=3, per_seconds=60)
    barrier = threading.Barrier(4)
    outcomes: list[str] = []

    def worker() -> None:
        barrier.wait()
        try:
            limiter.acquire()
            outcomes.append("ok")
        except RateLimitExceeded:
            outcomes.append("limited")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["limited", "ok", "ok", "ok"]
