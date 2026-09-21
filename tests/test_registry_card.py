"""Cross-file version consistency for the MCP Registry card.

The gate in publish-mcp.yml fires only on v* tags; between releases the card
(server.json), the lockfile self-entry and the README ownership token can
quietly drift away from pyproject.toml (this exact class shipped once: uv.lock
said 0.7.0 while pyproject said 0.9.0). These tests pin the agreement at every
commit, not only at tag time.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'^version = "(.*)"$', text, flags=re.MULTILINE)
    assert found, "pyproject.toml lost its version line"
    return found.group(1)


def test_server_json_card_versions_match_pyproject() -> None:
    card = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    version = _pyproject_version()
    assert card["version"] == version
    assert card["packages"][0]["version"] == version


def test_uv_lock_self_version_matches_pyproject() -> None:
    text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    found = re.search(r'name = "ironharness"\nversion = "([^"]+)"', text)
    assert found, "uv.lock lost its self entry for ironharness"
    assert found.group(1) == _pyproject_version()


def test_readme_carries_the_registry_ownership_token() -> None:
    card = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    token = f"mcp-name: {card['name']}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert token in readme, "README.md is missing the MCP Registry ownership token"
