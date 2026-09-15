"""IH-62: timeout classification invariant.

TimeoutError is an OSError subclass: in every runner, an `except TimeoutError`
arm must be declared BEFORE any `except OSError`/`except Exception` arm in the
same try statement, otherwise a wall-clock timeout is silently misclassified
as infra ("failed to talk to the board") instead of error_kind=timeout.
Enforced by parsing each runner module with ast: for every Try node that has
an OSError/Exception handler, a TimeoutError handler must appear earlier in
the same try (or the try must have no OSError handler at all).
"""

from __future__ import annotations

import ast
from pathlib import Path

RUNNERS = sorted((Path(__file__).resolve().parents[1] / "src" / "ironbench").glob("runner*.py"))


def _is_oserror_or_broad(handler: ast.ExceptHandler) -> bool:
    names = []
    node = handler.type
    if isinstance(node, ast.Name):
        names.append(node.id)
    elif isinstance(node, ast.Attribute):
        names.append(node.attr)
    elif isinstance(node, ast.Tuple):
        names.extend(elt.id for elt in node.elts if isinstance(elt, ast.Name))
    return any(n in ("OSError", "Exception", "BaseException") for n in names)


def test_timeout_handler_precedes_oserror_in_runners():
    for path in RUNNERS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            has_oserror_arm = False
            timeout_arm_seen = False
            for handler in node.handlers:
                t = handler.type
                names = []
                if isinstance(t, ast.Name):
                    names.append(t.id)
                elif isinstance(t, ast.Attribute):
                    names.append(t.attr)
                elif isinstance(t, ast.Tuple):
                    names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
                if any(n == "TimeoutError" for n in names):
                    timeout_arm_seen = True
                if has_oserror_arm and not timeout_arm_seen and any(
                    n in ("OSError", "Exception", "BaseException") for n in names
                ):
                    raise AssertionError(
                        f"{path.name}:{handler.lineno}: TimeoutError handler must be "
                        "declared before OSError/Exception in the same try (IH-62)"
                    )
                if any(n in ("OSError", "Exception", "BaseException") for n in names):
                    has_oserror_arm = True