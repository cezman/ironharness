"""Тесты файловой песочницы (этап 1, задача 4): границы, квоты, журнал."""

import os

import pytest

from io_core import FileSandbox, JsonlJournal, read_events
from io_core.errors import QuotaExceeded, SandboxViolation


@pytest.fixture()
def box(tmp_path):
    return FileSandbox(tmp_path / "sandbox", max_bytes=100, max_files=3)


def test_write_read_roundtrip(box):
    box.write_file("a/b.txt", b"hello")
    assert box.read_file("a/b.txt") == b"hello"
    assert box.list_dir() == ["a", "a/b.txt"]


def test_no_overwrite_by_default(box):
    box.write_file("f.txt", b"1")
    with pytest.raises(FileExistsError):
        box.write_file("f.txt", b"2")
    box.write_file("f.txt", b"22", overwrite=True)
    assert box.read_file("f.txt") == b"22"


def test_dotdot_escape_blocked(box):
    with pytest.raises(SandboxViolation):
        box.write_file("../escape.txt", b"x")


def test_deep_dotdot_escape_blocked(box):
    box.write_file("a/b.txt", b"x")
    with pytest.raises(SandboxViolation):
        box.read_file("a/../../../etc/passwd")


def test_absolute_path_blocked(box, tmp_path):
    with pytest.raises(SandboxViolation):
        box.write_file(str(tmp_path.parent / "outside.txt"), b"x")


@pytest.mark.skipif(os.name != "posix", reason="симлинки на Windows требуют привилегий")
def test_symlink_escape_blocked(box, tmp_path):
    link = box.root / "link"
    os.symlink(tmp_path, link)
    with pytest.raises(SandboxViolation):
        box.read_file("link/secret.txt")


def test_bytes_quota(box):
    box.write_file("big.bin", b"x" * 90)
    with pytest.raises(QuotaExceeded):
        box.write_file("other.bin", b"y" * 20)


def test_files_quota(box):
    box.write_file("f1.txt", b"1")
    box.write_file("f2.txt", b"2")
    box.write_file("f3.txt", b"3")  # ровно на лимите — можно
    with pytest.raises(QuotaExceeded):
        box.write_file("f4.txt", b"4")


def test_overwrite_replaces_bytes_not_adds(box):
    box.write_file("big.bin", b"x" * 90, overwrite=True)
    box.write_file("big.bin", b"y" * 10, overwrite=True)
    assert box.read_file("big.bin") == b"y" * 10


def test_delete(box):
    box.write_file("f.txt", b"1")
    box.delete_file("f.txt")
    assert box.list_dir() == []


def test_operations_are_journaled(tmp_path):
    jpath = tmp_path / "session.jsonl"
    with JsonlJournal(jpath, actor="test") as jr:
        box = FileSandbox(tmp_path / "sandbox", max_bytes=100, max_files=5, on_event=jr)
        box.write_file("f.txt", b"data")
        box.read_file("f.txt")
        box.delete_file("f.txt")
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["file_write", "file_read", "file_delete"]


def test_parallel_writes_cannot_exceed_quota(tmp_path):
    # IH-12: две параллельные записи по 600 КБ при лимите 1 МБ — ровно одна
    # проходит: проверка квоты и запись атомарны (раньше успевали обе).
    import threading

    box = FileSandbox(tmp_path / "sb", max_bytes=1_000_000, max_files=10)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(n: int) -> None:
        barrier.wait()
        try:
            box.write_file(f"{n}.bin", b"x" * 600_000, overwrite=True)
            results.append("ok")
        except QuotaExceeded:
            results.append("quota")

    threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "quota"]
