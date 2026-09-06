import firmbench
import io_core
from io_core.mcp_server import echo


def test_packages_importable():
    assert io_core.__version__
    assert firmbench.__version__


def test_echo_tool():
    assert echo("ping") == "ping"
