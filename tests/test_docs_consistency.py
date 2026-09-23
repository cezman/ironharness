"""Doc-consistency pins (IH-124): the docs drift silently because no test
compared them to the code. Each test here ties one documented surface to its
code source, so the next tool/env-var/asset change turns a doc gap red
instead of leaving it to a manual review round.

Covered here:
- README (EN/RU) and the skill's tool map list every registered MCP tool
  (the audit found serial_monitor missing from all three after the count
  became "33 tools");
- the why-docs never advertise `ironbench ...` as a console script (there is
  none by design - pyproject ships the io-core wheel only);
- every LLM_* env var the agent reads is documented in both READMEs
  (LLM_ALLOW_LOCAL is the SSRF-boundary switch - undocumented was the worst);
- ops task descriptions only promise what build_task_prompt actually renders
  (the "url and sha256 below" promise pointed at the assets block, which is
  never part of the prompt).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from io_core.mcp_server import mcp
from ironbench.ops_tasks import load_ops_tasks

REPO = Path(__file__).resolve().parents[1]
OPS_TASKS = load_ops_tasks()


def _registry_names() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


@pytest.mark.parametrize(
    ("doc", "marker"),
    [("README.md", "## Agent tools (MCP)"), ("README.ru.md", "## Инструменты агента (MCP)")],
)
def test_readme_tool_list_covers_the_registry(doc, marker):
    section = (REPO / doc).read_text(encoding="utf-8").split(marker)[1].split("\n## ")[0]
    missing = [name for name in sorted(_registry_names()) if not re.search(rf"\b{name}\b", section)]
    assert missing == [], f"{doc}: tools missing from the README list"


def test_skill_tool_map_covers_the_registry():
    text = (REPO / "skills" / "ironharness" / "SKILL.md").read_text(encoding="utf-8")
    missing = [name for name in sorted(_registry_names()) if not re.search(rf"\b{name}\b", text)]
    assert missing == [], "skills/ironharness/SKILL.md: tools missing from the tool map"


@pytest.mark.parametrize("doc", ["docs/why.md", "docs/why.ru.md"])
def test_why_docs_do_not_advertise_a_missing_console_script(doc):
    # `ironbench solve` reads like a console script; there is none by design
    # (pyproject ships the io-core wheel only) - the docs must point at
    # `python -m ironbench.cli ...`
    text = (REPO / doc).read_text(encoding="utf-8")
    offending = [span for span in re.findall(r"`([^`]+)`", text) if span.startswith("ironbench ")]
    assert offending == [], f"{doc}: console-script style commands that do not exist"


@pytest.mark.parametrize("doc", ["README.md", "README.ru.md"])
def test_readme_documents_every_llm_env_var(doc):
    text = (REPO / doc).read_text(encoding="utf-8")
    agent_source = (REPO / "src" / "ironbench" / "agent.py").read_text(encoding="utf-8")
    env_names = sorted(set(re.findall(r'"(LLM_[A-Z_]+)"', agent_source)))
    assert env_names, "the env-var scan found nothing - the regex rotted"
    missing = [name for name in env_names if name not in text]
    assert missing == [], f"{doc}: LLM env vars read by agent.py but not documented"


@pytest.mark.parametrize("task", OPS_TASKS, ids=lambda t: t.name)
def test_ops_description_promises_are_backed_by_the_prompt(task):
    from ironbench.ops_run import build_task_prompt

    build_task_prompt(task, port="COM9", assets={})  # must not need the assets
    promised = ("sha256" in task.description.lower()) or ("url" in task.description.lower())
    assert not promised, (
        f"{task.name}: the description promises url/sha256 but build_task_prompt "
        "renders description + dossier + notes only - the assets block never "
        "reaches the agent"
    )
