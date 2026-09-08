"""Тесты журнала и реплеера (этап 1, задача 2).

Сценарий-эталон: сессия на loop:// с журналом → по логу воспроизводится
та же сессия. Отдельно проверяем перенарезку чтений и strict-режим записей.
"""

import pytest

from io_core import JsonlJournal, ReplayMismatch, ReplayTransport, SerialTransport, read_events


def record(tmp_path, script):
    """Пишет сессию на loop:// в журнал и возвращает путь к файлу журнала."""
    jpath = tmp_path / "session.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, SerialTransport(
        "loop://", timeout=0.5, on_event=jr
    ) as t:
        script(t)
    return jpath


def test_journal_records_events(tmp_path):
    jpath = record(tmp_path, lambda t: (t.write(b"ping"), t.read(4)))
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["open", "write", "read", "close"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert all(e["actor"] == "test" for e in events)
    assert events[2]["data_hex"] == b"ping".hex()


def test_replay_roundtrip(tmp_path):
    jpath = record(tmp_path, lambda t: (t.write(b"ping"), t.read(4)))
    with ReplayTransport.from_file(jpath) as rp:
        rp.write(b"ping")
        assert rp.read(4) == b"ping"


def test_replay_rechunked_reads(tmp_path):
    def script(t):
        t.write(b"abcdef")
        assert t.read(3) == b"abc"
        assert t.read(3) == b"def"

    jpath = record(tmp_path, script)
    with ReplayTransport.from_file(jpath) as rp:
        rp.write(b"abcdef")
        assert rp.read(6) == b"abcdef"


def test_replay_read_line_across_chunks(tmp_path):
    def script(t):
        t.write(b"ab\ncd\n")
        assert t.read_line() == b"ab\n"
        assert t.read_line() == b"cd\n"

    jpath = record(tmp_path, script)
    with ReplayTransport.from_file(jpath) as rp:
        assert rp.read_line() == b"ab\n"
        assert rp.read_line() == b"cd\n"


def test_replay_strict_write_mismatch(tmp_path):
    jpath = record(tmp_path, lambda t: (t.write(b"ping"), t.read(4)))
    with ReplayTransport.from_file(jpath) as rp, pytest.raises(ReplayMismatch):
        rp.write(b"XXXX")


def test_replay_lenient_ignores_writes(tmp_path):
    jpath = record(tmp_path, lambda t: (t.write(b"ping"), t.read(4)))
    with ReplayTransport.from_file(jpath, strict=False) as rp:
        rp.write(b"XXXX")
        assert rp.read(4) == b"ping"
