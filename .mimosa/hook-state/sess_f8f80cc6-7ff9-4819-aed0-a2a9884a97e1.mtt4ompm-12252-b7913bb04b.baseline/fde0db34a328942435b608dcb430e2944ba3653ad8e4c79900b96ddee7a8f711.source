"""Тесты отчёта ironbench (pass@k, JSON + HTML)."""

from __future__ import annotations

import json

from ironbench.report import (
    aggregate,
    build_report,
    class_profile,
    load_results,
    render_html,
    write_report,
)


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


def test_class_profile_aggregates_by_tag():
    meta = {
        "blink": {"tags": ["io"], "level": 1},
        "frame-corrupt": {"tags": ["resilience"], "level": 4},
        "protocol-retry": {"tags": ["protocol", "resilience"], "level": 3},
    }
    records = [
        {"model": "m", "task": "blink", "solved": True},
        {"model": "m", "task": "protocol-retry", "solved": False},
        {"model": "m", "task": "frame-corrupt", "solved": True},
        {"model": "m", "task": "секретная-задача", "solved": True},  # без мета — мимо профиля
    ]
    profile = class_profile(records, meta)
    assert profile["m"]["io"] == {"attempts": 1, "solved": 1, "success_rate": 1.0}
    # protocol-retry с двумя тегами попадает в оба класса
    assert profile["m"]["resilience"]["attempts"] == 2
    assert profile["m"]["resilience"]["success_rate"] == 0.5
    assert "секретная-задача" not in json.dumps(profile)


def test_report_profile_missing_meta_is_compatible(tmp_path):
    # старая кампания без карты тегов: отчёт строится, профиль пустой
    write_results(tmp_path, [{"model": "m", "task": "x", "attempt": 1, "solved": True}])
    report = build_report(tmp_path)
    assert report["class_profile"] == {}
    assert report["groups"][0]["tags"] == []
    assert report["groups"][0]["level"] is None


def test_report_html_renders_profile_table(tmp_path):
    solve_dir = tmp_path / "camp"
    write_results(
        solve_dir,
        [
            {"model": "m", "task": "blink", "attempt": 1, "solved": True},
            {"model": "m", "task": "frame-corrupt", "attempt": 1, "solved": False},
        ],
    )
    task_meta = {
        "blink": {"tags": ["io"], "level": 1},
        "frame-corrupt": {"tags": ["resilience"], "level": 4},
    }
    json_path, html_path = write_report(solve_dir, tmp_path / "out", task_meta)
    html = html_path.read_text(encoding="utf-8")
    assert "Профиль по классам" in html
    assert "resilience" in html and "0/1 (0%)" in html
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["class_profile"]["m"]["io"]["solved"] == 1
