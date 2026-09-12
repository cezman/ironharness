"""IH-31 report honesty tests: infra attempts (environment failures) must
not count as model failures; unparseable results.jsonl lines are skipped
and reported, never crash the report; results.jsonl writes are atomic
(concurrent readers never see a truncated file); a re-run cleans stale
attempt artifacts of a longer previous campaign.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from ironbench.cli import (
    _replace_results_atomically,
    agent_solve_results,
    clean_stale_attempts,
)
from ironbench.report import build_report, load_results, render_html, render_leaderboard
from test_ironbench_report import write_results


def test_infra_attempts_excluded_from_success_rate():
    records = [
        # one live solve, one infra collapse: the model solved EVERY live run
        {"model": "m", "task": "t", "attempt": 1, "solved": True, "error_kind": "none"},
        {"model": "m", "task": "t", "attempt": 2, "solved": False, "error_kind": "infra"},
    ]
    (stats,) = aggregate_list(records)
    assert stats.attempts == 2
    assert stats.infra_failures == 1
    assert stats.success_rate == 1.0  # solved 1 / live 1, NOT 0.5
    assert stats.passed is True
    assert stats.incomplete is False


def aggregate_list(records):
    from ironbench.report import aggregate

    return aggregate(records)


def test_all_infra_pair_is_incomplete_and_excluded_from_pass_at_k(tmp_path):
    write_results(
        tmp_path,
        [
            {"model": "m", "task": "broken-env", "attempt": 1, "solved": False, "error_kind": "infra"},
            {"model": "m", "task": "broken-env", "attempt": 2, "solved": False, "error_kind": "infra"},
            {"model": "m", "task": "ok-task", "attempt": 1, "solved": True, "error_kind": "none"},
        ],
    )
    report = build_report(tmp_path)
    # pass@k counts only live pairs: 1 of 1 solved = 1.0 (broken-env says
    # nothing about the model - counting it would fake a 0.5)
    assert report["pass_at_k"] == 1.0
    broken = next(g for g in report["groups"] if g["task"] == "broken-env")
    assert broken["incomplete"] is True
    assert broken["success_rate"] is None
    assert broken["passed"] is False
    assert broken["infra_failures"] == 2


def test_solved_infra_attempt_is_not_counted_as_infra():
    # a solve that PASSED but is tagged infra is contradictory data; it
    # counts as a live solve (an infra exclusion may never erase a success)
    records = [{"model": "m", "task": "t", "attempt": 1, "solved": True, "error_kind": "infra"}]
    (stats,) = aggregate_list(records)
    assert stats.infra_failures == 0
    assert stats.success_rate == 1.0
    assert stats.incomplete is False


def test_unparseable_lines_skipped_and_reported(tmp_path):
    # one truncated line (killed process) must not destroy every campaign
    solve_dir = tmp_path / "camp"
    solve_dir.mkdir(parents=True)
    good = json.dumps({"model": "m", "task": "t", "attempt": 1, "solved": True})
    (solve_dir / "results.jsonl").write_text(
        good + "\n" + '{"model": "m", "task": "trunc' + "\n" + "[1, 2]\n",
        encoding="utf-8",
    )
    records, unparseable = load_results(solve_dir)
    assert len(records) == 1
    assert len(unparseable) == 1
    assert unparseable[0]["lines"] == 2
    report = build_report(solve_dir)
    assert report["unparseable"] == unparseable
    html = render_html(report)
    assert "unparseable results.jsonl lines skipped" in html
    assert "results.jsonl" in html


def test_html_shows_na_and_honesty_note_for_infra(tmp_path):
    write_results(
        tmp_path,
        [
            {"model": "m", "task": "broken", "attempt": 1, "solved": False, "error_kind": "infra"},
        ],
    )
    report = build_report(tmp_path)
    html = render_html(report)
    assert ">n/a<" in html
    assert "infra (environment failures) are excluded" in html
    assert 'class="pass"' not in html and 'class="fail"' not in html


def test_leaderboard_excludes_incomplete_pairs(tmp_path):
    write_results(
        tmp_path,
        [
            {"model": "m", "task": "broken", "attempt": 1, "solved": False, "error_kind": "infra"},
            {"model": "m", "task": "ok", "attempt": 1, "solved": True, "error_kind": "none"},
        ],
    )
    report = build_report(tmp_path)
    html = render_leaderboard(report)
    # the model's pass@k is 100% over the single live pair, not 50% over two
    assert "<td>100%</td>" in html
    assert "infra\nattempts (environment failures) excluded" in html


def test_results_write_is_atomic_under_concurrent_readers(tmp_path):
    # two campaigns writing the same results.jsonl (last wins, by design):
    # a concurrent reader must NEVER see a truncated/mixed file - the
    # tmp+os.replace protocol guarantees a whole-file view.
    import os

    class R:
        def __init__(self, n: int, model: str):
            self.task = "t"
            self.attempt = 1
            self.solved = False
            self.iterations = n
            self.duration_sec = 1.0
            self.error = None
            self.error_kind = "run"
            self.model = model

    target = tmp_path / "t" / "results.jsonl"
    target.parent.mkdir(parents=True)
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            if not target.exists():
                continue
            try:
                for line in target.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rec = json.loads(line)  # must never see a half file
                        assert rec["model"] in ("A", "B")
            except PermissionError:
                # Windows: the reader's open handle collided with os.replace -
                # an access conflict on a WHOLE file, not a torn write
                continue
            except (ValueError, AssertionError) as e:
                errors.append(e)
                return

    watcher = threading.Thread(target=reader, daemon=True)
    watcher.start()

    def writer(tag: str):
        try:
            for i in range(30):
                payload = "".join(
                    json.dumps(
                        {"task": "t", "attempt": 1, "solved": False, "iterations": i,
                         "model": tag, "duration_sec": 1.0, "error": None,
                         "error_kind": "run"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                # per-writer tmp, like the production code (per-process pid)
                tmp = target.with_name(f"{target.name}.{tag}.tmp")
                tmp.write_text(payload, encoding="utf-8")
                try:
                    os.replace(tmp, target)
                except PermissionError:
                    # Windows: a concurrent reader handle blocks the replace;
                    # the tmp file stays whole - retry next round
                    continue
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(t,), daemon=True) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    stop.set()
    watcher.join(timeout=5)
    assert errors == [], f"reader observed a torn write: {errors[:2]}"


def test_agent_solve_results_atomic_and_clean(tmp_path, monkeypatch):
    # integration through the CLI helper: stale attempt-* dirs of a longer
    # previous campaign are removed, results.jsonl lands atomically. The
    # recorder on os.replace pins the PRODUCTION path - a mutation replacing
    # the atomic rename with a direct write turns this test red.
    task_dir = tmp_path / "camp" / "mytask"
    stale = task_dir / "attempt-1"
    stale.mkdir(parents=True)
    (stale / "iter-9.main.py").write_text("old junk", encoding="utf-8")

    from ironbench.agent import AttemptResult
    from ironbench.tasks import Task

    fake_result = AttemptResult(
        task="mytask", solved=True, iterations=2, duration_sec=1.0,
        work_dir=task_dir / "attempt-1" / "work", attempt=1,
        error=None, error_kind="none",
    )
    monkeypatch.setattr(
        "ironbench.cli.agent_solve", lambda *a, **k: [fake_result]
    )
    task = Task(name="mytask", description="", directory=tmp_path, scenario=None,
                entry="main.py", timeout_sec=5, expect=(), fail=(), target="unix")

    class Cfg:
        model = "fake-model"

    replaces: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(src, dst, *a, **k):
        replaces.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr("ironbench.cli.os.replace", recording_replace)
    agent_solve_results(task, Cfg(), attempts=1, solve_dir=tmp_path / "camp")
    assert not stale.exists() or not (stale / "iter-9.main.py").exists()
    results_file = task_dir / "results.jsonl"
    records = [json.loads(l) for l in results_file.read_text("utf-8").splitlines() if l.strip()]
    assert records[0]["model"] == "fake-model"
    # the atomic-rename protocol ran over the production results.jsonl path
    assert any(dst.endswith("results.jsonl") for _, dst in replaces), (
        "results.jsonl was not written through the atomic replace protocol"
    )
    assert not list(task_dir.glob("results.jsonl.*.tmp")), "tmp file left behind"


def test_replace_retries_when_reader_holds_the_file(tmp_path, monkeypatch):
    # The Windows hole (review round 1 blocker): a concurrent reader holding
    # an open handle makes os.replace fail with PermissionError. Without the
    # bounded retry the campaign dies after burning all attempts and
    # results.jsonl silently keeps the PREVIOUS campaign's data. The first
    # replace is forced to fail; the retry must land the data.
    target = tmp_path / "results.jsonl"
    target.write_text('{"old": "campaign"}', encoding="utf-8")
    tmp = tmp_path / "results.jsonl.999.tmp"
    tmp.write_text('{"new": "campaign"}', encoding="utf-8")
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "reader holds the handle")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr("ironbench.cli.os.replace", flaky_replace)
    monkeypatch.setattr("ironbench.cli.time.sleep", lambda s: None)
    _replace_results_atomically(tmp, target)
    assert json.loads(target.read_text("utf-8")) == {"new": "campaign"}


def test_replace_raises_loudly_after_exhaustion(tmp_path, monkeypatch):
    # the negative path: retries exhausted -> a loud PermissionError, never a
    # silent keep-the-old-data (a gate without a failing negative path is
    # decorative)
    target = tmp_path / "results.jsonl"
    target.write_text('{"old": "campaign"}', encoding="utf-8")
    tmp = tmp_path / "results.jsonl.999.tmp"
    tmp.write_text('{"new": "campaign"}', encoding="utf-8")
    calls = {"n": 0}

    def always_busy(src, dst, *a, **k):
        calls["n"] += 1
        raise PermissionError(13, "held forever")

    monkeypatch.setattr("ironbench.cli.os.replace", always_busy)
    monkeypatch.setattr("ironbench.cli.time.sleep", lambda s: None)
    with pytest.raises(PermissionError):
        _replace_results_atomically(tmp, target)
    assert calls["n"] == 6  # 1 initial + 5 retries
    assert json.loads(target.read_text("utf-8")) == {"old": "campaign"}  # old data intact


def test_clean_stale_attempts_removes_stale_tmp_files(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "results.jsonl.4242.tmp").write_text("killed mid-campaign", encoding="utf-8")
    n = clean_stale_attempts(task_dir)
    assert n == 1
    assert not (task_dir / "results.jsonl.4242.tmp").exists()


def test_clean_stale_attempts_keeps_results_and_journal(tmp_path):
    task_dir = tmp_path / "task"
    (task_dir / "attempt-1").mkdir(parents=True)
    (task_dir / "attempt-2" / "work").mkdir(parents=True)
    (task_dir / "results.jsonl").write_text("{}\n", encoding="utf-8")
    (task_dir / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    n = clean_stale_attempts(task_dir)
    assert n == 2
    assert (task_dir / "results.jsonl").is_file()
    assert (task_dir / "journal.jsonl").is_file()
    assert clean_stale_attempts(tmp_path / "nonexistent") == 0
