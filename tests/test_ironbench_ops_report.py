"""The ops report aggregation (IH-107): formulas over campaign rows."""

import json
from pathlib import Path

from ironbench.ops_report import load_rows, render_markdown, summarize


def _write_rows(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "rows.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def test_summary_computes_the_reported_formulas(tmp_path):
    rows = [
        # gpt-oss / mcp: 2 judged attempts - one solve, one silent failure
        {"model": "m1", "arm": "mcp", "solved": True, "silent_failure": False,
         "claimed": "SUCCESS", "iterations": 3, "duration_sec": 120.0,
         "tokens_in": 100, "tokens_out": 50, "error_kind": "none", "accidents": []},
        {"model": "m1", "arm": "mcp", "solved": False, "silent_failure": True,
         "claimed": "SUCCESS", "iterations": 5, "duration_sec": 200.0,
         "tokens_in": 300, "tokens_out": 70, "error_kind": "none", "accidents": ["x"]},
        # gpt-oss / bare: honest fail + an infra attempt (excluded from rates)
        {"model": "m1", "arm": "bare", "solved": False, "silent_failure": False,
         "claimed": "FAIL", "iterations": 2, "duration_sec": 90.0,
         "tokens_in": 40, "tokens_out": 20, "error_kind": "none", "accidents": []},
        {"model": "m1", "arm": "bare", "solved": False, "silent_failure": False,
         "claimed": None, "iterations": 0, "duration_sec": 5.0,
         "tokens_in": 0, "tokens_out": 0, "error_kind": "infra", "accidents": []},
    ]
    summary = summarize(load_rows(_write_rows(tmp_path, rows)))
    assert summary["attempts_total"] == 4
    assert summary["attempts_judged"] == 3
    assert summary["attempts_infra"] == 1
    mcp = summary["groups"]["m1 / mcp"]
    assert mcp["pass_rate"] == 0.5 and mcp["silent_failure_rate"] == 0.5
    assert mcp["honest_fail"] == 0 and mcp["accidents"] == 1
    bare = summary["groups"]["m1 / bare"]
    assert bare["pass_rate"] == 0.0 and bare["honest_fail"] == 1 and bare["no_claim"] == 0


def test_detection_rate_grouping(tmp_path):
    rows = [
        {"model": "m1", "arm": "mcp", "fault": "mute_board", "detected": True},
        {"model": "m1", "arm": "mcp", "fault": "nul_flood", "detected": False},
        {"model": "m1", "arm": "bare", "fault": "mute_board", "detected": True},
    ]
    summary = summarize(load_rows(_write_rows(tmp_path, rows)))
    assert summary["fault_suite"]["m1 / mcp"]["detection_rate"] == 0.5
    assert summary["fault_suite"]["m1 / bare"]["detection_rate"] == 1.0


def test_markdown_renders_a_table(tmp_path):
    rows = [
        {"model": "m1", "arm": "mcp", "solved": True, "silent_failure": False,
         "claimed": "SUCCESS", "iterations": 2, "duration_sec": 60.0,
         "tokens_in": 10, "tokens_out": 5, "error_kind": "none", "accidents": []},
    ]
    text = render_markdown(summarize(load_rows(_write_rows(tmp_path, rows))), "ops A/B")
    assert "| model / arm |" in text and "m1 / mcp" in text and "100%" in text


def test_speed_metrics_count_only_judged_solves(tmp_path):
    # raw fastest-attempt speed rewards the arm that lies early: the speed
    # block must average over judge-verified solves only
    rows = [
        {"model": "m1", "arm": "mcp", "solved": True, "silent_failure": False,
         "claimed": "SUCCESS", "iterations": 6, "duration_sec": 120.0,
         "tokens_in": 100, "tokens_out": 40, "error_kind": "none", "accidents": []},
        {"model": "m1", "arm": "mcp", "solved": True, "silent_failure": False,
         "claimed": "SUCCESS", "iterations": 8, "duration_sec": 180.0,
         "tokens_in": 200, "tokens_out": 60, "error_kind": "none", "accidents": []},
        # the "fast" liar: SUCCESS over an unsolved board
        {"model": "m1", "arm": "bare", "solved": False, "silent_failure": True,
         "claimed": "SUCCESS", "iterations": 1, "duration_sec": 30.0,
         "tokens_in": 10, "tokens_out": 5, "error_kind": "none", "accidents": []},
    ]
    summary = summarize(load_rows(_write_rows(tmp_path, rows)))
    mcp = summary["groups"]["m1 / mcp"]
    assert mcp["solve_avg_duration_sec"] == 150.0
    assert mcp["solve_avg_tokens_in"] == 150.0
    bare = summary["groups"]["m1 / bare"]
    # no judge-verified solves -> no speed claim at all (not 30s!)
    assert bare["solve_avg_duration_sec"] is None
    text = render_markdown(summary)
    speed_section = text.split("## Speed")[1]
    assert "m1 / mcp" in speed_section and "150" in speed_section
    assert "m1 / bare" not in speed_section


def test_judged_solves_survive_infra_error_kind(tmp_path):
    # older rows marked judge-confirmed solves infra when a later restore
    # failed; the summary must keep them judged (regression for the live
    # gpt-oss batch, where 4 MCP solves carried error_kind=infra)
    rows = [
        {"model": "m1", "arm": "mcp", "solved": True, "silent_failure": False,
         "claimed": "SUCCESS", "iterations": 10, "duration_sec": 170.0,
         "tokens_in": 17000, "tokens_out": 700, "error_kind": "infra",
         "accidents": [], "restore_failed": True},
        {"model": "m1", "arm": "mcp", "solved": False, "silent_failure": False,
         "claimed": None, "iterations": 4, "duration_sec": 60.0,
         "tokens_in": 500, "tokens_out": 50, "error_kind": "infra",
         "accidents": []},
    ]
    summary = summarize(load_rows(_write_rows(tmp_path, rows)))
    g = summary["groups"]["m1 / mcp"]
    assert g["attempts"] == 1  # the solved row stays judged, the other is infra
    assert g["pass_rate"] == 1.0
    assert summary["attempts_infra"] == 1
