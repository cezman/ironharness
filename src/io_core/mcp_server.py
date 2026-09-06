"""Точка входа MCP-сервера io-core.

Каждый инструмент io-core обязан быть доступен агенту как MCP tool (конвенция MCP-first).
На этапе 0 — только echo (проверка связности), транспорты добавятся на этапе 1.
"""

from mcp.server import MCPServer

mcp = MCPServer("io-core")


@mcp.tool()
def echo(text: str) -> str:
    """Возвращает текст обратно. Служебный инструмент для проверки работы MCP."""
    return text


if __name__ == "__main__":
    mcp.run()  # stdio
