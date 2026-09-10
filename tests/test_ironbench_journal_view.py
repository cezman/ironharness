"""Journal viewer tests (IH-8): the page is a faithful and inert view of the
JSONL operation journal. Faithful: events, their order, facets and honest
skipped/omitted counters. Inert: journal text (firmware/agent-controlled) can
never become HTML or terminate the data block - every "</" sequence is escaped
in the embedded JSON and the table is rendered via textContent only."""

from __future__ import annotations

import json
import re
from pathlib import Path

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
    hostile = '</script><img src=x onerror=alert(1)><script>alert(2)</script>'
    journal = write_journal(
        tmp_path, [{"ts": 1.0, "seq": 1, "actor": "a", "kind": "read", "conn": "esp", "data_hex": hostile}]
    )
    out_file, _ = write_view(journal, tmp_path / "view.html")
    html_text = out_file.read_text(encoding="utf-8")
    # the data block survived: exactly the two real </script> closers (the
    # ih-data block and the viewer script), nothing opened by journal text
    assert html_text.count("</script") == 2
    # the hostile sequence cannot appear unescaped in the file
    assert "</script><img" not in html_text
    # and the JSON round-trips to the original payload text ( \/ decodes to / )
    view = read_view_data(html_text)
    assert view["rows"][0]["p"]["data_hex"] == hostile


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
