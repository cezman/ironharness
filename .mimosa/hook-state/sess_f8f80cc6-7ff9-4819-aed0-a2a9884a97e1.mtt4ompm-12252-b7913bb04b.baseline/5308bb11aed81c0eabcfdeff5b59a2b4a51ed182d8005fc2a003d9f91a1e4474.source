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
