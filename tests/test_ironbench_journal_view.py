"""Journal viewer tests (IH-8): the page is a faithful and inert view of the
JSONL operation journal. Faithful: events, their order, facets and honest
skipped/omitted counters. Inert: journal text (firmware/agent-controlled) can
never become HTML or terminate the data block - every "</" sequence is escaped
in the embedded JSON and the table is rendered via textContent only."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from io_core.journal import JsonlJournal
from ironbench.cli import main as cli_main
from ironbench.journal_view import build_view, write_view

DATA_RE = re.compile(
    r'<script id="ih-data" type="application/json">(.*?)</script>', re.DOTALL
)


def write_journal(tmp_path: Path, lines: list) -> Path:
    journal = tmp_path / "journal.jsonl"
    text = "\n".join(
        json.dumps(line, ensure_ascii=False) if isinstance(line, dict) else line
        for line in lines
    )
    journal.write_text(text + "\n", encoding="utf-8")
    return journal


def read_view_data(html_text: str) -> dict:
    return json.loads(DATA_RE.search(html_text).group(1))


def test_events_ordered_by_ts_payload_normalized(tmp_path):
    journal = write_journal(
        tmp_path,
        [
            {"ts": 100.5, "seq": 1, "actor": "io_core", "kind": "write", "conn": "esp", "data_hex": "aa"},
            {"ts": 99.0, "seq": 1, "actor": "ironbench", "kind": "task_start", "task": "blink"},
            {"ts": 100.5, "seq": 2, "actor": "io_core", "kind": "read", "conn": "esp", "data_hex": "bb"},
        ],
    )
    view = build_view(journal)
    assert view["file_events"] == 3 and view["skipped_lines"] == 0
    assert [r["kind"] for r in view["rows"]] == ["task_start", "write", "read"]
    assert view["rows"][1]["conn"] == "esp" and view["rows"][1]["time"]
    # service keys became columns, payload keeps the rest
    assert "data_hex" in view["rows"][1]["p"] and "ts" not in view["rows"][1]["p"]
    assert view["kinds"] == ["read", "task_start", "write"]
    assert view["actors"] == ["io_core", "ironbench"]
    assert view["conns"] == ["esp"]


def test_unparseable_lines_counted_not_dropped(tmp_path):
    journal = write_journal(
        tmp_path,
        [
            {"ts": 1.0, "seq": 1, "actor": "a", "kind": "write"},
            "this is not json",
            "[1, 2, 3]",
        ],
    )
    view = build_view(journal)
    assert view["file_events"] == 1 and view["skipped_lines"] == 2
    out_file, _ = write_view(journal, tmp_path / "view.html")
    assert "2 unparseable line(s) skipped" in out_file.read_text(encoding="utf-8")


def test_hostile_payload_stays_inert(tmp_path):
    hostile = '</script><img src=x onerror=alert(1)><script>alert(2)</script><!-- <script>'
    journal = write_journal(
        tmp_path, [{"ts": 1.0, "seq": 1, "actor": "a", "kind": "read", "conn": "esp", "data_hex": hostile}]
    )
    out_file, _ = write_view(journal, tmp_path / "view.html")
    html_text = out_file.read_text(encoding="utf-8")
    # the data block survived: exactly the two real </script> closers (the
    # ih-data block and the viewer script), nothing opened by journal text
    assert html_text.count("</script") == 2
    # the hostile sequences cannot appear unescaped in the file
    assert "</script><img" not in html_text
    assert "<!-- <script>" not in html_text
    # and the JSON round-trips to the original payload text ( \/ and \u0021 decode)
    view = read_view_data(html_text)
    assert view["rows"][0]["p"]["data_hex"] == hostile


def test_token_in_source_name_is_never_expanded(tmp_path):
    # F1 regression: a journal NAME containing a template token must not get
    # expanded into the page (token expansion = HTML injection into title/h1)
    journal = tmp_path / "evil__DATA_JSON____SUMMARY__.jsonl"
    journal.write_text(
        json.dumps(
            {"ts": 1.0, "seq": 1, "actor": "a", "kind": "read", "x": "<!-- <script>alert(1)</script>"}
        )
        + "\n",
        encoding="utf-8",
    )
    out_file, _ = write_view(journal, tmp_path / "view.html")
    html_text = out_file.read_text(encoding="utf-8")
    head = html_text[: DATA_RE.search(html_text).start()]
    assert "evil__DATA_JSON____SUMMARY__.jsonl" in head  # the name itself, escaped
    # the payload is data, not markup: its script text stays inside the blob
    assert "alert(1)" not in head
    assert html_text.count("</script") == 2
    view = read_view_data(html_text)
    assert view["rows"][0]["p"]["x"] == "<!-- <script>alert(1)</script>"


def test_non_finite_floats_do_not_kill_the_page(tmp_path):
    # F3 regression: bare NaN/Infinity from json.dumps is invalid JSON for a
    # browser's JSON.parse - the dynamic view would silently die client-side
    journal = write_journal(
        tmp_path,
        [{"ts": 1.0, "seq": 1, "actor": "a", "kind": "op", "v": float("nan"), "w": float("inf")}],
    )
    out_file, _ = write_view(journal, tmp_path / "view.html")
    view = read_view_data(out_file.read_text(encoding="utf-8"))
    assert view["rows"][0]["p"]["v"] == "nan" and view["rows"][0]["p"]["w"] == "inf"


def test_deeply_nested_payload_survives(tmp_path):
    # _sanitize is iterative: a payload nested deeper than the interpreter
    # stack must not raise RecursionError (json's C scanner handles it fine)
    leaf: dict = {"v": float("nan")}
    for _ in range(2000):
        leaf = {"d": leaf}
    journal = write_journal(
        tmp_path, [{"ts": 1.0, "seq": 1, "actor": "a", "kind": "op", "deep": leaf}]
    )
    assert cli_main(["journal", str(journal)]) == 0
    out_file = tmp_path / "journal.jsonl.view.html"
    view = read_view_data(out_file.read_text(encoding="utf-8"))
    node = view["rows"][0]["p"]["deep"]
    for _ in range(2000):
        node = node["d"]
    assert node["v"] == "nan"


def test_row_time_survives_extreme_timestamps(tmp_path):
    # F5 regression: pre-epoch ts (OSError on Windows), overflow ts and
    # non-numeric ts must not crash the view - the row keeps a string time
    journal = write_journal(
        tmp_path,
        [
            {"ts": -1.0, "seq": 1, "actor": "a", "kind": "op"},
            {"ts": 1e30, "seq": 2, "actor": "a", "kind": "op"},
            {"ts": "junk", "seq": 3, "actor": "a", "kind": "op"},
        ],
    )
    view = build_view(journal)
    assert all(isinstance(r["time"], str) for r in view["rows"])
    # rows sorted by ts with junk falling back to 0.0: [-1.0, junk, 1e30]
    assert view["rows"][1]["time"] == ""  # non-numeric ts: no time, row kept
    assert view["rows"][2]["time"] == "1e+30"  # overflow: raw value kept
    out_file, _ = write_view(journal, tmp_path / "view.html")
    assert "3 event(s)" in out_file.read_text(encoding="utf-8")


def test_view_refuses_to_overwrite_the_journal(tmp_path):
    # F2 regression: --out-file equal to the journal must not destroy the
    # journal (the artifact "no log = didn't happen" protects)
    journal = write_journal(tmp_path, [{"ts": 1.0, "seq": 1, "actor": "a", "kind": "op"}])
    before = journal.read_bytes()
    with pytest.raises(ValueError):
        write_view(journal, journal)
    assert cli_main(["journal", str(journal), "--out-file", str(journal)]) == 2
    assert journal.read_bytes() == before


def test_truncation_keeps_head_and_tail_and_states_it(tmp_path):
    base = 1_700_000_000.0  # safely above the epoch: local-time rendering works
    journal = write_journal(
        tmp_path,
        [{"ts": base + i, "seq": i, "actor": "a", "kind": "op", "n": i} for i in range(30)],
    )
    view = build_view(journal, max_events=10)
    assert view["file_events"] == 30
    # the cap bounds the shown rows: head half + tail half, middle omitted
    assert view["shown_events"] == 10 and view["omitted_events"] == 20
    assert view["rows"][0]["p"]["n"] == 0 and view["rows"][-1]["p"]["n"] == 29
    assert view["span"] == [view["rows"][0]["time"], view["rows"][-1]["time"]]
    out_file, _ = write_view(journal, tmp_path / "view.html", max_events=10)
    assert "20 middle event(s) omitted" in out_file.read_text(encoding="utf-8")


def test_empty_journal_renders(tmp_path):
    journal = write_journal(tmp_path, [])
    view = build_view(journal)
    assert view["rows"] == [] and view["span"] == []
    out_file, _ = write_view(journal, tmp_path / "view.html")
    assert "0 event(s)" in out_file.read_text(encoding="utf-8")


def test_written_by_real_journal(tmp_path):
    journal_path = tmp_path / "journal.jsonl"
    with JsonlJournal(journal_path, actor="session") as journal:
        journal("write", {"conn": "esp", "data_hex": "aa"})
        journal("task_result", {"task": "blink", "passed": True})
    view = build_view(journal_path)
    assert [r["kind"] for r in view["rows"]] == ["write", "task_result"]
    assert view["rows"][0]["actor"] == "session"


def test_cli_writes_view_next_to_journal(tmp_path):
    journal = write_journal(tmp_path, [{"ts": 1.0, "seq": 1, "actor": "a", "kind": "op"}])
    assert cli_main(["journal", str(journal)]) == 0
    out_file = tmp_path / "journal.jsonl.view.html"
    assert out_file.is_file()
    assert read_view_data(out_file.read_text(encoding="utf-8"))["file_events"] == 1


def test_cli_explicit_out_file(tmp_path):
    journal = write_journal(tmp_path, [{"ts": 1.0, "seq": 1, "actor": "a", "kind": "op"}])
    out = tmp_path / "sub" / "view.html"
    assert cli_main(["journal", str(journal), "--out-file", str(out)]) == 0
    assert out.is_file()


def test_cli_missing_journal_is_an_error(tmp_path):
    # negative path: a missing journal is a clean CLI error, not a traceback
    assert cli_main(["journal", str(tmp_path / "nope.jsonl")]) == 2
