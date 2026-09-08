"""ironbench: бенчмарк для firmware-агентов (симулятор, золотые задачи, агентский цикл, отчёты)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ironharness")  # единый источник версии — метаданные пакета
except PackageNotFoundError:  # запущен из исходников без установки
    __version__ = "0.3.0"
