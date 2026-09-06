"""io-core: безопасный I/O-слой для агентов (транспорты, журнал, политики, MCP)."""

from io_core.journal import JsonlJournal, read_events
from io_core.modbus_sim import ModbusSimServer
from io_core.modbus_transport import ModbusTransport
from io_core.replay import ReplayMismatch, ReplayTransport
from io_core.serial_transport import SerialTransport

__version__ = "0.1.0"

__all__ = [
    "JsonlJournal",
    "ModbusSimServer",
    "ModbusTransport",
    "ReplayMismatch",
    "ReplayTransport",
    "SerialTransport",
    "read_events",
]
