"""Publishing the ironbench leaderboard to a git pages branch (IH-6).

`ironbench report --publish` pushes index.html + data.json to the pages
branch (default gh-pages) of a git remote - GitHub Pages serves the
leaderboard straight from that branch, so no other infrastructure is needed.
The caller's worktree is never touched: everything happens in a throwaway
clone of the remote.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path


class PublishError(RuntimeError):
    """The leaderboard cannot be pushed (git missing, no remote, push refused)."""


def _ensure(proc: subprocess.CompletedProcess, what: str) -> None:
    if proc.returncode != 0:
        raise PublishError(f"{what} failed: {proc.stderr.strip()}")


def publish_report(
    report: dict,
    html: str,
    *,
    repo: Path,
    remote: str = "origin",
    branch: str = "gh-pages",
) -> str:
    """Push the leaderboard page to `branch` on `remote`; returns the pushed sha.

    `report` lands as data.json, `html` as index.html - both at the branch
    root, which is what Pages "deploy from branch" serves. An existing branch
    gets a new commit on top; a missing one is created orphan.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", remote],
        capture_output=True, text=True, shell=False, check=False,
    )
    if proc.returncode != 0:
        raise PublishError(f"no git remote {remote!r} in {repo}: {proc.stderr.strip()}")
    url = proc.stdout.strip()

    probe = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", url, branch],
        capture_output=True, text=True, shell=False, check=False,
    )
    if probe.returncode not in (0, 2):
        raise PublishError(f"cannot reach {remote!r} ({url}): {probe.stderr.strip()}")
    branch_exists = probe.returncode == 0

    tmp = Path(tempfile.mkdtemp(prefix="ironbench-pages-"))
    try:
        _ensure(
            subprocess.run(["git", "-C", str(tmp), "init"],
                           capture_output=True, text=True, shell=False, check=False),
            "git init",
        )
        _ensure(
            subprocess.run(["git", "-C", str(tmp), "remote", "add", "origin", url],
                           capture_output=True, text=True, shell=False, check=False),
            "git remote add",
        )
        if branch_exists:
            _ensure(
                subprocess.run(["git", "-C", str(tmp), "fetch", "--depth", "1", "origin", branch],
                               capture_output=True, text=True, shell=False, check=False),
                f"fetch of {branch}",
            )
            _ensure(
                subprocess.run(["git", "-C", str(tmp), "checkout", "-B", branch, "FETCH_HEAD"],
                               capture_output=True, text=True, shell=False, check=False),
                f"checkout of {branch}",
            )
        else:
            _ensure(
                subprocess.run(["git", "-C", str(tmp), "checkout", "--orphan", branch],
                               capture_output=True, text=True, shell=False, check=False),
                f"checkout of {branch}",
            )

        (tmp / "index.html").write_text(html, encoding="utf-8")
        (tmp / "data.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        who = subprocess.run(["git", "-C", str(tmp), "config", "user.email"],
                             capture_output=True, text=True, shell=False, check=False)
        if not who.stdout.strip():
            subprocess.run(["git", "-C", str(tmp), "config", "user.name", "ironbench"],
                           capture_output=True, text=True, shell=False, check=False)
            subprocess.run(["git", "-C", str(tmp), "config", "user.email",
                            "ironbench@users.noreply.github.com"],
                           capture_output=True, text=True, shell=False, check=False)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        status = subprocess.run(["git", "-C", str(tmp), "status", "--porcelain"],
                                capture_output=True, text=True, shell=False, check=False)
        if status.stdout.strip():
            _ensure(
                subprocess.run(["git", "-C", str(tmp), "add", "-A"],
                               capture_output=True, text=True, shell=False, check=False),
                "git add",
            )
            _ensure(
                subprocess.run(["git", "-C", str(tmp), "commit", "-m", f"leaderboard: publish {stamp}"],
                               capture_output=True, text=True, shell=False, check=False),
                "git commit",
            )
        _ensure(
            subprocess.run(["git", "-C", str(tmp), "push", "origin", f"HEAD:{branch}"],
                           capture_output=True, text=True, shell=False, check=False),
            f"push to {remote}/{branch}",
        )
        return subprocess.run(["git", "-C", str(tmp), "rev-parse", "HEAD"],
                              capture_output=True, text=True, shell=False, check=False).stdout.strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
