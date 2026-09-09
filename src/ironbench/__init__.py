"""ironbench: the firmware-agent benchmark (simulator, golden tasks, agent loop, reports)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ironharness")  # single source of version - package metadata
except PackageNotFoundError:  # running from sources without installation
    __version__ = "0.3.0"
