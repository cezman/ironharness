"""IH-63: journal rotation - a long session with a background reader must
not grow journal.jsonl without bound. Past max_bytes the live file rotates
into numbered parts (newest part = .1); parts beyond max_files are dropped.
read_events_chain reads the live file and all parts oldest-first.
"""

import json

from io_core.journal import JsonlJournal, read_events_chain


def test_journal_rotates_at_max_bytes(tmp_path):
    j = JsonlJournal(tmp_path / "j.jsonl", actor="t", max_bytes=600, max_files=3)
    for i in range(20):
        j("event", {"i": i})
    j.close()
    parts = sorted(p.name for p in tmp_path.glob("j.jsonl*") if p.is_file() and not p.name.endswith(".lock"))
    assert "j.jsonl" in parts and any(".1" in p for p in parts), parts
    total = sum(
        1
        for part in tmp_path.glob("j.jsonl*")
        for line in part.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert total == 20, f"events lost in rotation: {total}"


def test_journal_rotation_drops_parts_beyond_max_files(tmp_path):
    j = JsonlJournal(tmp_path / "j.jsonl", actor="t", max_bytes=400, max_files=2)
    for i in range(60):
        j("event", {"i": i})
    j.close()
    parts = sorted(p.name for p in tmp_path.glob("j.jsonl.*") if not p.name.endswith(".lock"))
    assert len(parts) <= 2, f"too many rotated parts kept: {parts}"


def test_read_events_chain_reads_parts_in_order(tmp_path):
    """IH-63: chronological order across rotated parts - the oldest part
    (highest index) first, then the live file. Rotation shifts older parts
    to higher indices, so .1 always holds the NEWEST archived events."""
    p1 = tmp_path / "j.jsonl.1"
    p1.write_text(json.dumps({"kind": "event", "n": 1}) + "\n", encoding="utf-8")
    live = tmp_path / "j.jsonl"
    live.write_text(
        json.dumps({"kind": "event", "n": 2}) + "\n" + json.dumps({"kind": "event", "n": 3}) + "\n",
        encoding="utf-8",
    )
    events = read_events_chain(live)
    assert [e.get("n") for e in events if e.get("n") is not None] == [1, 2, 3]


def test_read_events_chain_multiple_parts_chronological(tmp_path):
    """IH-63: with two rotated parts the chain reads .2, .1, then the live
    file - rotation shifts older content to higher indices."""
    p2 = tmp_path / "j.jsonl.2"
    p1 = tmp_path / "j.jsonl.1"
    live = tmp_path / "j.jsonl"
    p2.write_text(json.dumps({"kind": "event", "n": 1}) + "\n", encoding="utf-8")
    p1.write_text(json.dumps({"kind": "event", "n": 2}) + "\n", encoding="utf-8")
    live.write_text(json.dumps({"kind": "event", "n": 3}) + "\n", encoding="utf-8")
    events = read_events_chain(live)
    assert [e.get("n") for e in events if e.get("n") is not None] == [1, 2, 3]


def test_read_events_chain_missing_file_is_empty(tmp_path):
    assert read_events_chain(tmp_path / "absent.jsonl") == []
