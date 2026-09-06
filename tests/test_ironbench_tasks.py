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
