"""io-core: безопасный I/O-слой для агентов (транспорты, журнал, политики, MCP)."""

from io_core.serial_transport import SerialTransport

__version__ = "0.1.0"

__all__ = ["SerialTransport"]
