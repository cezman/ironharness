import importlib

import io_core
import ironbench
from io_core.mcp_server import echo


def test_packages_importable():
    assert io_core.__version__
    assert ironbench.__version__


def test_echo_tool():
    assert echo("ping") == "ping"


def test_version_fallback_survives_none_metadata(monkeypatch):
    """IH-118 / live incident 2026-09-21: a broken dist-info (no RECORD) made
    importlib.metadata.version return None on Python 3.14 instead of raising
    PackageNotFoundError - __version__ silently became None and every
    truthiness consumer broke. The fallback must catch None too, in both
    packages."""
    monkeypatch.setattr("importlib.metadata.version", lambda _name: None)

    mod = importlib.reload(io_core)
    assert mod.__version__, "io_core fallback must not yield None"
    bench = importlib.reload(ironbench)
    assert bench.__version__, "ironbench fallback must not yield None"

    # restore the real metadata-resolved values for the other tests
    importlib.reload(io_core)
    importlib.reload(ironbench)
