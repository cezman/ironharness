"""Тесты отчёта ironbench (pass@k, JSON + HTML)."""

from __future__ import annotations

import json

from ironbench.report import aggregate, build_report, load_results, render_html, write_report


def write_results(solve_dir, records):
    solve_dir.mkdir(parents=True, exist_ok=True)
    (solve_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def test_load_and_aggregate(tmp_path):
    write_results(
        tmp_path,
        [
            {"model": "m1", "task": "blink", "attempt": 1, "solved": True, "iterations": 2, "duration_sec": 100},
            {"model": "m1", "task": "blink", "attempt": 2, "solved": False, "iterations": 5, "duration_sec": 200},
            {"model": "m1", "task": "uart-echo", "attempt": 1, "solved": True, "iterations": 1, "duration_sec": 50},
        ],
    )
    stats = aggregate(load_results(tmp_path))
    assert [(s.model, s.task, s.attempts, s.solved) for s in stats] == [
        ("m1", "blink", 2, 1),
        ("m1", "uart-echo", 1, 1),
    ]
    blink = stats[0]
    assert blink.success_rate == 0.5
    assert blink.avg_iterations == 3.5
    assert blink.avg_duration == 150.0
    assert blink.passed is True


def test_build_report_pass_at_k(tmp_path):
    write_results(
        tmp_path,
        [
            {"model": "m", "task": "a", "attempt": 1, "solved": True, "iterations": 1},
            {"model": "m", "task": "b", "attempt": 1, "solved": False, "iterations": 5},
        ],
    )
    report = build_report(tmp_path)
    assert report["pass_at_k"] == 0.5  # решена хотя бы одна пара из двух
    assert report["tasks"] == ["a", "b"]


def test_build_report_empty(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    report = build_report(tmp_path)
    assert report["groups"] == []
    assert report["pass_at_k"] == 0.0


def test_write_report_files(tmp_path):
    solve_dir = tmp_path / "camp"
    write_results(
        solve_dir,
        [
            {"model": "qwen3.5-9b", "task": "blink", "attempt": 1, "solved": True, "iterations": 1},
            {"model": "qwen3.5-9b", "task": "protocol", "attempt": 1, "solved": False, "iterations": 5},
        ],
    )
    json_path, html_path = write_report(solve_dir, tmp_path / "out")
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert {g["task"] for g in report["groups"]} == {"blink", "protocol"}
    html = html_path.read_text(encoding="utf-8")
    assert "blink" in html and "protocol" in html
    assert 'class="pass"' in html and 'class="fail"' in html
    assert render_html(report).startswith("<!doctype html>")
