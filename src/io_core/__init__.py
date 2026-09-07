"""io-core: безопасный I/O-слой для агентов (транспорты, журнал, политики, MCP)."""

from io_core.errors import (
    OperationTimeout,
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
from io_core.replay import ReplayMismatch, ReplayTransport
from io_core.serial_transport import SerialTransport
from io_core.session import Session
from io_core.verify import expect_read, write_and_expect

__version__ = "0.1.0"

__all__ = [
    "DeadlineTransport",
    "EspFlasher",
    "FileSandbox",
    "JsonlJournal",
    "ModbusSimServer",
    "ModbusTransport",
    "MqttTransport",
    "OperationTimeout",
    "QuotaExceeded",
    "RateLimitExceeded",
    "RateLimitedTransport",
    "RateLimiter",
    "ReplayMismatch",
    "ReplayTransport",
    "SandboxViolation",
    "SerialTransport",
    "Session",
    "VerificationError",
    "expect_read",
    "read_events",
    "write_and_expect",
]
