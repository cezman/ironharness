"""Tests of the leaderboard publish path (report --publish): render + git push.

Git is exercised against local bare repositories - no network, no real remote.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from ironbench.cli import main as cli_main
from ironbench.publish import PublishError, publish_report
from ironbench.report import build_report, render_leaderboard

META = {"blink": {"tags": ["io"], "level": 1}, "uart-echo": {"tags": ["io"], "level": 1}}


def write_results(solve_dir, records):
    solve_dir.mkdir(parents=True, exist_ok=True)
    (solve_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def sample_report(tmp_path, solved_uart=False):
    write_results(
        tmp_path / "solve",
        [
            {"model": "m1", "task": "blink", "attempt": 1, "solved": True, "iterations": 2, "duration_sec": 10},
            {"model": "m1", "task": "uart-echo", "attempt": 1, "solved": solved_uart, "iterations": 3, "duration_sec": 20},
        ],
    )
    return build_report(tmp_path / "solve", META)


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, shell=False, check=False)


@pytest.fixture
def source_repo(tmp_path):
    """A plain git repo (no commits needed) with the bare 'origin' next to it."""
    origin = tmp_path / "origin.git"
    src = tmp_path / "src"
    assert git("init", "--bare", str(origin)).returncode == 0
    assert git("init", str(src)).returncode == 0
    assert git("-C", str(src), "remote", "add", "origin", origin.as_posix()).returncode == 0
    return src, origin


def test_render_leaderboard_table(tmp_path):
    html = render_leaderboard(sample_report(tmp_path), generated="2026-09-09 00:00 UTC")
    assert "<title>ironbench leaderboard</title>" in html
    assert "<td>m1</td>" in html
    assert "<th>io</th>" in html
    assert "2026-09-09 00:00 UTC" in html
    # one of the two (model, task) pairs solved -> pass@k 50%, io cell 1/2
    assert "<td>50%</td>" in html
    assert "1/2 (50%)" in html


def test_render_leaderboard_empty(tmp_path):
    (tmp_path / "solve").mkdir()
    html = render_leaderboard(build_report(tmp_path / "solve"))
    assert "<td>m1</td>" not in html
    assert "<th>Model</th>" in html


def test_publish_creates_orphan_branch(tmp_path, source_repo):
    src, origin = source_repo
    report = sample_report(tmp_path)
    sha = publish_report(report, render_leaderboard(report), repo=src)
    assert len(sha) == 40
    html = git("--git-dir", str(origin), "show", "refs/heads/gh-pages:index.html").stdout
    assert "<td>m1</td>" in html
    data = json.loads(
        git("--git-dir", str(origin), "show", "refs/heads/gh-pages:data.json").stdout
    )
    assert data["pass_at_k"] == report["pass_at_k"]


def test_publish_updates_existing_branch(tmp_path, source_repo):
    src, origin = source_repo
    first = publish_report(sample_report(tmp_path), "<html>old</html>", repo=src)
    second = publish_report(sample_report(tmp_path, solved_uart=True), "<html>new</html>", repo=src)
    assert first != second
    count = git("--git-dir", str(origin), "rev-list", "--count", "refs/heads/gh-pages").stdout
    assert count.strip() == "2"
    html = git("--git-dir", str(origin), "show", "refs/heads/gh-pages:index.html").stdout
    assert "<html>new</html>" in html


def test_publish_no_changes_is_a_noop(tmp_path, source_repo):
    src, origin = source_repo
    first = publish_report(sample_report(tmp_path), "<html>same</html>", repo=src)
    second = publish_report(sample_report(tmp_path), "<html>same</html>", repo=src)
    assert first == second
    count = git("--git-dir", str(origin), "rev-list", "--count", "refs/heads/gh-pages").stdout
    assert count.strip() == "1"


def test_publish_requires_remote(tmp_path):
    src = tmp_path / "src"
    assert git("init", str(src)).returncode == 0
    with pytest.raises(PublishError, match="no git remote"):
        publish_report({}, "<html></html>", repo=src)


def test_publish_unreachable_remote(tmp_path):
    src = tmp_path / "src"
    missing = (tmp_path / "missing.git").as_posix()
    assert git("init", str(src)).returncode == 0
    assert git("-C", str(src), "remote", "add", "origin", missing).returncode == 0
    with pytest.raises(PublishError, match="cannot reach"):
        publish_report({}, "<html></html>", repo=src)


def test_publish_preserves_unrelated_files(tmp_path, source_repo):
    src, origin = source_repo
    publish_report(sample_report(tmp_path), "<html>v1</html>", repo=src)
    # someone (e.g. Pages config) put a foreign file on the branch
    seed = tmp_path / "seed"
    assert git("clone", "--depth", "1", "-b", "gh-pages", origin.as_posix(), str(seed)).returncode == 0
    (seed / "CNAME").write_text("bench.example.com", encoding="utf-8")
    assert git("-C", str(seed), "config", "user.name", "tester").returncode == 0
    assert git("-C", str(seed), "config", "user.email", "tester@example.com").returncode == 0
    assert git("-C", str(seed), "add", "CNAME").returncode == 0
    assert git("-C", str(seed), "commit", "-m", "seed cname").returncode == 0
    assert git("-C", str(seed), "push", "origin", "HEAD:gh-pages").returncode == 0
    # the next publish keeps it and updates our own files
    publish_report(sample_report(tmp_path, solved_uart=True), "<html>v2</html>", repo=src)
    cname = git("--git-dir", str(origin), "show", "refs/heads/gh-pages:CNAME").stdout
    assert cname == "bench.example.com"
    html = git("--git-dir", str(origin), "show", "refs/heads/gh-pages:index.html").stdout
    assert "<html>v2</html>" in html


def test_cli_report_publish(tmp_path, source_repo, capsys, monkeypatch):
    src, origin = source_repo
    write_results(
        tmp_path / "solve",
        [{"model": "m1", "task": "blink", "attempt": 1, "solved": True, "iterations": 1, "duration_sec": 5}],
    )
    monkeypatch.chdir(src)
    rc = cli_main(
        [
            "report", "--out", str(tmp_path / "out"), "--solve-dir", str(tmp_path / "solve"),
            "--publish", "--remote", "origin", "--pages-branch", "gh-pages",
        ]
    )
    assert rc == 0
    assert "published: origin/gh-pages" in capsys.readouterr().out
    html = git("--git-dir", str(origin), "show", "refs/heads/gh-pages:index.html").stdout
    assert "<td>m1</td>" in html


def test_cli_report_publish_failure_is_reported(tmp_path, capsys, monkeypatch):
    src = tmp_path / "src"
    assert git("init", str(src)).returncode == 0
    (tmp_path / "solve").mkdir()
    monkeypatch.chdir(src)
    rc = cli_main(
        ["report", "--out", str(tmp_path / "out"), "--solve-dir", str(tmp_path / "solve"), "--publish"]
    )
    assert rc == 1
    assert "publish failed" in capsys.readouterr().out
