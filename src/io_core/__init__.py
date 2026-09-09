"""io-core: безопасный I/O-слой для агентов (транспорты, журнал, политики, MCP)."""

from importlib.metadata import PackageNotFoundError, version

from io_core.errors import (
    OperationTimeout,
    PolicyViolation,
    QuotaExceeded,
    RateLimitExceeded,
    SandboxViolation,
    VerificationError,
)
from io_core.esp_flash import EspFlasher
from io_core.file_sandbox import FileSandbox
from io_core.journal import JsonlJournal, read_events
from io_core.limits import DeadlineTransport, RateLimitedTransport, RateLimiter
from io_core.modbus_sim import ModbusSimServer
from io_core.modbus_transport import ModbusTransport
from io_core.mqtt_transport import MqttTransport
from io_core.policy import AccessPolicy
from io_core.replay import ReplayMismatch, ReplaySession, ReplayTransport
from io_core.serial_transport import SerialTransport
from io_core.session import Session
from io_core.verify import expect_read, write_and_expect

try:
    __version__ = version("ironharness")  # единый источник версии — метаданные пакета
except PackageNotFoundError:  # запущен из исходников без установки
    __version__ = "0.3.0"

__all__ = [
    "AccessPolicy",
    "DeadlineTransport",
    "EspFlasher",
    "FileSandbox",
    "JsonlJournal",
    "ModbusSimServer",
    "ModbusTransport",
    "MqttTransport",
    "OperationTimeout",
    "PolicyViolation",
    "QuotaExceeded",
    "RateLimitExceeded",
    "RateLimitedTransport",
    "RateLimiter",
    "ReplayMismatch",
    "ReplaySession",
    "ReplayTransport",
    "SandboxViolation",
    "SerialTransport",
    "Session",
    "VerificationError",
    "expect_read",
    "read_events",
    "write_and_expect",
]
