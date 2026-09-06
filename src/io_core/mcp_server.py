"""MCP-сервер ironharness: инструменты io-core для агентов (этап 1, задача 7).

Конвенция MCP-first: агенты работают с I/O только через этот сервер,
и каждая операция сессии автоматически пишется в журнал.

Конфигурация через окружение:
    IRONHARNESS_HOME     — база для журнала и песочницы (по умолчанию ~/.ironharness)
    IRONHARNESS_SANDBOX  — корень файловой песочницы (по умолчанию IRONHARNESS_HOME/sandbox)
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server import MCPServer

from io_core.session import Session

mcp = MCPServer("ironharness")

_session: Session | None = None


def get_session() -> Session:
    global _session
    if _session is None:
        home = Path(os.environ.get("IRONHARNESS_HOME", Path.home() / ".ironharness"))
        _session = Session(
            journal_path=home / "journal.jsonl",
            sandbox_root=os.environ.get("IRONHARNESS_SANDBOX", home / "sandbox"),
            actor="mcp",
        )
    return _session


def reset_session() -> None:
    """Сбрасывает сессию (для тестов и смены окружения)."""
    global _session
    if _session is not None:
        _session.close()
    _session = None


@mcp.tool()
def echo(text: str) -> str:
    """Возвращает текст обратно. Служебный инструмент для проверки работы MCP."""
    return text


# --- serial (бинарные данные — hex-строками, JSON-дружелюбно) ---


@mcp.tool()
def serial_open(name: str, port: str, baudrate: int = 115200, timeout: float = 1.0) -> str:
    """Открывает именованный serial-порт (COM3, /dev/ttyUSB0, loop:// для теста)."""
    get_session().serial_open(name, port, baudrate=baudrate, timeout=timeout)
    return f"ok: serial {name!r} -> {port}"


@mcp.tool()
def serial_write(name: str, data_hex: str) -> int:
    """Пишет байты в serial-порт; data_hex — hex-строка (например 48656c6c6f)."""
    return get_session().serial_write(name, data_hex)


@mcp.tool()
def serial_read(name: str, size: int = 64) -> str:
    """Читает до size байт из serial-порта; возвращает hex-строку ("" — таймаут)."""
    return get_session().serial_read(name, size)


@mcp.tool()
def serial_read_line(name: str, max_len: int = 256) -> str:
    """Читает строку до \\n из serial-порта; возвращает hex-строку."""
    return get_session().serial_read_line(name, max_len)


# --- modbus ---


@mcp.tool()
def modbus_open(name: str, host: str, port: int = 502, device_id: int = 1) -> str:
    """Открывает именованное Modbus TCP-соединение."""
    get_session().modbus_open(name, host, port=port, device_id=device_id)
    return f"ok: modbus {name!r} -> {host}:{port}"


@mcp.tool()
def modbus_read(name: str, address: int, count: int = 1) -> list[int]:
    """Читает count holding-регистров начиная с address."""
    return get_session().modbus_read(name, address, count)


@mcp.tool()
def modbus_write(name: str, address: int, values: list[int]) -> str:
    """Пишет holding-регистры: один элемент → FC6, несколько → FC16."""
    get_session().modbus_write(name, address, values)
    return f"ok: записано {len(values)} регистр(ов) с адреса {address}"


# --- файлы (песочница) ---


@mcp.tool()
def file_write(path: str, content: str) -> int:
    """Пишет текстовый файл (utf-8) внутрь песочницы; выход за песочницу запрещён."""
    return get_session().file_write(path, content)


@mcp.tool()
def file_read(path: str) -> str:
    """Читает текстовый файл из песочницы."""
    return get_session().file_read(path)


@mcp.tool()
def file_list(path: str = ".") -> list[str]:
    """Список файлов и каталогов песочницы (относительные пути)."""
    return get_session().file_list(path)


@mcp.tool()
def file_delete(path: str) -> str:
    """Удаляет файл из песочницы."""
    get_session().file_delete(path)
    return f"ok: удалён {path}"


if __name__ == "__main__":
    mcp.run()  # stdio
