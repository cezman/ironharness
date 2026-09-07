"""Тесты загрузки задач ironbench из YAML."""

from __future__ import annotations

import pytest

from ironbench.tasks import load_task, load_tasks

VALID = """
name: blink
description: тестовая задача
timeout_sec: 15
scenario: scenario.yaml
expect:
  - 'blink 0: on'
fail:
  - 'Traceback'
"""


def write_task(tmp_path, content=VALID, dirname="blink"):
    d = tmp_path / dirname
    d.mkdir()
    (d / "task.yaml").write_text(content, encoding="utf-8")
    return d


def test_load_task_parses_fields(tmp_path):
    d = write_task(tmp_path)
    task = load_task(d)
    assert task.name == "blink"
    assert task.timeout_sec == 15
    assert task.expect == ("blink 0: on",)
    assert task.fail == ("Traceback",)
    assert task.directory == d


def test_load_task_defaults(tmp_path):
    d = write_task(tmp_path, "name: t1\n")
    task = load_task(d)
    assert task.timeout_sec == 30
    assert task.scenario is None  # нет scenario → раннер генерирует REPL-paste
    assert task.entry == "main.py"
    assert task.expect == ()
    assert task.stimulus == ()


def test_load_task_stimulus(tmp_path):
    content = "name: t1\nstimulus:\n  - delay: 500ms\n  - write-serial: \"hi\\n\"\n"
    task = load_task(write_task(tmp_path, content))
    assert task.stimulus == ({"delay": "500ms"}, {"write-serial": "hi\n"})


def test_load_task_stimulus_must_be_steps(tmp_path):
    d = write_task(tmp_path, "name: t1\nstimulus:\n  - just a string\n")
    with pytest.raises(ValueError, match="stimulus"):
        load_task(d)


def test_load_task_stimulus_unknown_step_key(tmp_path):
    d = write_task(tmp_path, "name: t1\nstimulus:\n  - write-seral: 'x'\n")
    with pytest.raises(ValueError, match="write-seral"):
        load_task(d)


def test_load_task_requires_name(tmp_path):
    d = write_task(tmp_path, "timeout_sec: 5\n")
    with pytest.raises(ValueError, match="name"):
        load_task(d)


def test_load_task_expect_must_be_strings(tmp_path):
    d = write_task(tmp_path, "name: t1\nexpect:\n  - 42\n")
    with pytest.raises(ValueError, match="expect"):
        load_task(d)


def test_load_tasks_sorted_and_skips_dirs_without_task(tmp_path):
    write_task(tmp_path, "name: bbb\n", dirname="bbb")
    write_task(tmp_path, "name: aaa\n", dirname="aaa")
    (tmp_path / "empty_dir").mkdir()
    tasks = load_tasks(tmp_path)
    assert [t.name for t in tasks] == ["aaa", "bbb"]


def test_load_tasks_missing_dir(tmp_path):
    with pytest.raises(ValueError, match="не найден"):
        load_tasks(tmp_path / "nope")


def test_load_task_tags_and_level(tmp_path):
    write_task(tmp_path, "name: aaa\ntags: [io, fsm]\nlevel: 3\n", dirname="aaa")
    task = load_task(tmp_path / "aaa")
    assert task.tags == ("io", "fsm")
    assert task.level == 3


def test_load_task_rejects_unknown_tag(tmp_path):
    write_task(tmp_path, "name: aaa\ntags: [роботы]\n", dirname="aaa")
    with pytest.raises(ValueError, match="неизвестные теги"):
        load_task(tmp_path / "aaa")


def test_load_task_rejects_duplicate_tags(tmp_path):
    write_task(tmp_path, "name: aaa\ntags: [io, io]\n", dirname="aaa")
    with pytest.raises(ValueError, match="дубликаты"):
        load_task(tmp_path / "aaa")


def test_load_task_rejects_bad_level(tmp_path):
    write_task(tmp_path, "name: aaa\nlevel: 9\n", dirname="aaa")
    with pytest.raises(ValueError, match="level"):
        load_task(tmp_path / "aaa")


def test_golden_tasks_have_tags_and_levels():
    from pathlib import Path

    tasks = load_tasks(Path(__file__).parents[1] / "src" / "ironbench" / "tasks")
    assert all(t.tags for t in tasks), "у всех золотых задач должен быть класс"
    assert all(t.level is not None for t in tasks), "у всех золотых задач должен быть уровень"


# --- золотые задачи репозитория: целостность описаний ---


def test_golden_tasks_all_load_and_include_expectations():
    from pathlib import Path

    tasks = load_tasks(Path(__file__).parents[1] / "src" / "ironbench" / "tasks")
    names = {t.name for t in tasks}
    assert {
        "blink",
        "uart-echo",
        "noisy-frames",
        "frame-corrupt",
        "uart-menu",
        "p-regulator",
        "pid-antiwindup",
        "system-id",
    } <= names


def test_frame_corrupt_stimulus_checksums_are_honest():
    # ретраи в стимуле обязаны нести корректный xor2, иначе эталон не решит задачу
    import re
    from pathlib import Path

    task = load_task(Path(__file__).parents[1] / "src" / "ironbench" / "tasks" / "frame-corrupt")
    checked = 0
    for step in task.stimulus:
        raw = str(step.get("write-serial", ""))
        m = re.fullmatch(r"#(\w+):(\w+):([0-9a-f]{2})\r?", raw)
        if not m:
            continue  # обрывок кадра без xor — законная часть сценария
        _fid, payload, x2 = m.groups()
        x = 0
        for ch in payload:
            x ^= ord(ch)
        assert f"{x:02x}" == x2, f"битая сумма в стимуле: {raw!r}"
        checked += 1
    assert checked >= 4
