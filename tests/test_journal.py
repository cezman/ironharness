"""Тесты журнала и реплеера (этап 1, задача 2).

Сценарий-эталон: сессия на loop:// с журналом → по логу воспроизводится
та же сессия. Отдельно проверяем перенарезку чтений и strict-режим записей.
"""

import pytest

from io_core import (
    JsonlJournal,
    ReplayMismatch,
    ReplaySession,
    ReplayTransport,
    SerialTransport,
    read_events,
)


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
        # strict replay reproduces the WHOLE session (IH-32): the recorded
        # write must be replayed too, or close() reports it as missing
        rp.write(b"ab\ncd\n")
        assert rp.read_line() == b"ab\n"
        assert rp.read_line() == b"cd\n"


def test_replay_strict_write_mismatch(tmp_path):
    jpath = record(tmp_path, lambda t: (t.write(b"ping"), t.read(4)))
    with ReplayTransport.from_file(jpath) as rp:
        with pytest.raises(ReplayMismatch):
            rp.write(b"XXXX")  # recorded write does not match
        rp.write(b"ping")  # the honest completion satisfies close()'s fullness check
        assert rp.read(4) == b"ping"


def test_replay_lenient_ignores_writes(tmp_path):
    jpath = record(tmp_path, lambda t: (t.write(b"ping"), t.read(4)))
    with ReplayTransport.from_file(jpath, strict=False) as rp:
        rp.write(b"XXXX")
        assert rp.read(4) == b"ping"


# --- IH-11: несколько писателей журнала и по-соединений реплей ---


def test_journal_two_writers_same_file(tmp_path):
    # Обещание «несколько сессий могут дописывать один файл»: каждая строка
    # доходит целой, порядок внутри каждого писателя сохранён по seq.
    # Межписательский порядок задаёт ts (seq у каждого писателя свой).
    jpath = tmp_path / "shared.jsonl"
    with JsonlJournal(jpath, actor="a") as ja, JsonlJournal(jpath, actor="b") as jb:
        for i in range(50):
            ja("ping", {"n": i, "payload": "x" * 120})
            jb("pong", {"n": i, "payload": "y" * 120})
    events = read_events(jpath)
    assert len(events) == 100
    for actor in ("a", "b"):
        seqs = [e["seq"] for e in events if e["actor"] == actor]
        assert len(seqs) == 50 and seqs == sorted(seqs)


def test_replay_session_legacy_journal_without_conn(tmp_path):
    # Журналы без conn (транспорты напрямую, записи до IH-11) реплеятся как
    # одно безымянное соединение — старое поведение целиком.
    jpath = record(tmp_path, lambda t: (t.write(b"ping"), t.read(4)))
    rs = ReplaySession.from_file(jpath)
    assert rs.order == ("",)
    with rs[""] as rp:
        rp.write(b"ping")
        assert rp.read(4) == b"ping"
