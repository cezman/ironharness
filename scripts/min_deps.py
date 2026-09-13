"""Prints the minimum dependency versions declared in pyproject.toml as
`name==floor` spec lines, one per line (IH-41). The CI min-deps job installs
exactly this output, so the declared floors can never silently drift from
what is actually tested. Exits non-zero when a dependency has no `>=` floor
or carries constructs the output cannot represent exactly (extras, environment
markers, epochs, wildcards) - a loud failure beats a silently wrong pin.
Boundary: only project.dependencies are checked; the [flash] extra and the
dev group (pytest, ruff) have no floor enforcement here.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_FLOOR_RE = re.compile(r"^([A-Za-z0-9_.-]+)>=([0-9][0-9a-zA-Z.]*)$")


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
        m = _FLOOR_RE.match(dep.strip())
        if not m:
            print(
                f"{dep!r}: expected exactly 'name>=<version>' (no extras, markers, "
                "epochs or ranges) - the min-deps job cannot pin it faithfully",
                file=sys.stderr,
            )
            status = 2
            continue
        print(f"{m.group(1)}=={m.group(2)}")
    return status


if __name__ == "__main__":
    sys.exit(main())
