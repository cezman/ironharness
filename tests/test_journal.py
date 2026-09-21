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


# --- IH-114: readers take the writer's sidecar lock ---


def test_read_events_takes_the_sidecar_lock(tmp_path):
    """IH-114: a reader that ignores the sidecar lock races the writer's
    rotation - on Windows replace() hits the reader's open handle (WinError 32,
    the event is lost), on POSIX the reader silently follows the rename.
    read_events must hold the same lock the writer rotates under."""
    import threading

    import io_core.journal as journal_mod

    jpath = tmp_path / "j.jsonl"
    jpath.write_text('{"ts": 1, "seq": 1, "actor": "a", "kind": "k"}\n', encoding="utf-8")
    lock_path = jpath.with_name("j.jsonl.lock")
    lock_path.touch()  # a writer exists -> its sidecar is there (created in the ctor)
    held = journal_mod._acquire_lock_file(lock_path)
    done = threading.Event()

    def read():
        journal_mod.read_events(jpath)
        done.set()

    th = threading.Thread(target=read, daemon=True)
    th.start()
    assert not done.wait(0.3), "read_events ignored the sidecar lock (IH-114)"
    journal_mod._release_lock_file(held)
    assert done.wait(5), "the reader never finished after the lock was released"


def test_read_events_chain_takes_the_sidecar_lock(tmp_path):
    import threading

    import io_core.journal as journal_mod

    jpath = tmp_path / "j.jsonl"
    (tmp_path / "j.jsonl.1").write_text(
        '{"ts": 1, "seq": 1, "actor": "a", "kind": "k1"}\n', encoding="utf-8"
    )
    jpath.write_text('{"ts": 2, "seq": 2, "actor": "a", "kind": "k2"}\n', encoding="utf-8")
    lock_path = jpath.with_name("j.jsonl.lock")
    lock_path.touch()
    held = journal_mod._acquire_lock_file(lock_path)
    done = threading.Event()

    def read():
        journal_mod.read_events_chain(jpath)
        done.set()

    th = threading.Thread(target=read, daemon=True)
    th.start()
    assert not done.wait(0.3), "read_events_chain ignored the sidecar lock (IH-114)"
    journal_mod._release_lock_file(held)
    assert done.wait(5), "the reader never finished after the lock was released"


def test_write_survives_a_live_reader_across_rotations(tmp_path):
    """IH-114 functional: a live reader hammering the journal while the writer
    rotates must not lose events (Windows: WinError 32 out of __call__)."""
    import threading

    import io_core.journal as journal_mod

    jpath = tmp_path / "j.jsonl"
    # max_files=50: nothing is pruned by design - the assertion counts every
    # event, not just the surviving tail of the rotation window
    j = journal_mod.JsonlJournal(jpath, actor="t", max_bytes=300, max_files=50)
    failures: list[OSError] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                journal_mod.read_events_chain(jpath)
            except OSError as e:  # the old-code failure mode on Windows
                failures.append(e)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        for i in range(40):
            j("e", {"i": i, "pad": "x" * 80})  # crosses max_bytes several times
    finally:
        stop.set()
        th.join(5)
    assert not failures, f"the writer failed under a live reader: {failures[:1]}"
    assert sum(1 for e in journal_mod.read_events_chain(jpath) if e["kind"] == "e") == 40


def test_rotation_waits_for_a_slow_reader_then_lands(tmp_path):
    """IH-114 no-wedge pin: a reader holding the sidecar lock delays the
    write, and the event still lands in the chain once the reader lets go."""
    import threading
    import time

    import io_core.journal as journal_mod

    jpath = tmp_path / "j.jsonl"
    j = journal_mod.JsonlJournal(jpath, actor="t", max_bytes=200)
    j("e", {"n": 0, "pad": "x" * 150})  # right below the limit
    held = journal_mod._acquire_lock_file(jpath.with_name("j.jsonl.lock"))

    def release():
        time.sleep(0.15)
        journal_mod._release_lock_file(held)

    th = threading.Thread(target=release)
    th.start()
    j("e", {"n": 1, "pad": "x" * 150})  # crosses max_bytes -> rotates under the held lock
    th.join(5)
    assert [e["n"] for e in journal_mod.read_events_chain(jpath)] == [0, 1]
