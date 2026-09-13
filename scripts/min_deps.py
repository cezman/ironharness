"""Prints the minimum dependency versions declared in pyproject.toml as
`name==floor` spec lines, one per line (IH-41). The CI min-deps job installs
exactly this output, so the declared floors can never silently drift from
what is actually tested. Exits non-zero when a dependency has no `>=` floor -
an unpinned dependency would make the min-deps job meaningless.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <path-to-pyproject.toml>", file=sys.stderr)
        return 2
    data = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", [])
    if not deps:
        print("no project.dependencies in pyproject.toml", file=sys.stderr)
        return 2
    status = 0
    for dep in deps:
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", dep.strip())
        if not m:
            print(f"unparseable dependency: {dep!r}", file=sys.stderr)
            status = 2
            continue
        name, spec = m.group(1), m.group(2)
        floor = re.search(r">=\s*([0-9][0-9a-zA-Z.]*)", spec)
        if not floor:
            print(f"{name}: no >= floor in spec {spec!r} - add one", file=sys.stderr)
            status = 2
            continue
        print(f"{name}=={floor.group(1)}")
    return status


if __name__ == "__main__":
    sys.exit(main())
