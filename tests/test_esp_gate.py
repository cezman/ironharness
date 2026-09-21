"""Negative paths of the live-flash gate (IH-29). Without
IRONHARNESS_ALLOW_REAL_FLASH=1, flash/erase must raise PermissionError before
anything touches esptool or the port, and the refusal itself must land in the
journal - an unauthorized flashing attempt leaves a trace. This file runs
WITHOUT the [flash] extra on purpose: the gate precedes the dependency check,
so the deny path is testable everywhere, and deleting the gate check would
turn these tests red on any machine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import io_core.esp_flash as esp_mod
from io_core import EspFlasher, JsonlJournal, Session, read_events


def journal_kinds(jpath: Path) -> list[str]:
    return [
        json.loads(line)["kind"]
        for line in jpath.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture(autouse=True)
def no_flag(monkeypatch):
    monkeypatch.delenv(esp_mod.ALLOW_REAL_FLASH_ENV, raising=False)


@pytest.fixture()
def bomb_connect(monkeypatch) -> list:
    """A connect_esp that must never be reached while the gate is closed."""
    calls: list = []

    def _bomb(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("esptool connect must not run while the gate is closed")

    monkeypatch.setattr(esp_mod, "connect_esp", _bomb, raising=False)
    return calls


@pytest.fixture()
def image(tmp_path: Path) -> Path:
    p = tmp_path / "fw.bin"
    p.write_bytes(b"\xe9")  # content is irrelevant: the gate fires first
    return p


def test_flash_denied_without_flag(tmp_path, image, bomb_connect):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(
        PermissionError, match="IRONHARNESS_ALLOW_REAL_FLASH"
    ):
        EspFlasher(on_event=jr).flash("COM7", image)
    assert bomb_connect == []  # the port was never touched
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["esp_denied"]
    assert events[0]["op"] == "flash"
    assert events[0]["port"] == "COM7"
    assert "IRONHARNESS_ALLOW_REAL_FLASH" in events[0]["error"]


def test_erase_denied_without_flag(tmp_path, bomb_connect):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(PermissionError):
        EspFlasher(on_event=jr).erase("COM7")
    assert bomb_connect == []
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["esp_denied"]
    assert events[0]["op"] == "erase"


def test_gate_rejects_anything_but_1(tmp_path, image, monkeypatch, bomb_connect):
    monkeypatch.setenv(esp_mod.ALLOW_REAL_FLASH_ENV, "0")
    with pytest.raises(PermissionError):
        EspFlasher().flash("COM7", image)
    monkeypatch.setenv(esp_mod.ALLOW_REAL_FLASH_ENV, "true")
    with pytest.raises(PermissionError):
        EspFlasher().flash("COM7", image)
    assert bomb_connect == []


def test_gate_precedes_the_esptool_check(tmp_path, image, monkeypatch):
    # without the flag the refusal is a PermissionError even when esptool is
    # missing - the safety verdict must not depend on an optional dependency
    monkeypatch.setattr(esp_mod, "_ESPTOOL_AVAILABLE", False)
    with pytest.raises(PermissionError, match="IRONHARNESS_ALLOW_REAL_FLASH"):
        EspFlasher().flash("COM7", image)
    with pytest.raises(PermissionError):
        EspFlasher().erase("COM7")


def test_flag_set_passes_the_gate(monkeypatch, tmp_path, image):
    # the counter-negative: with the flag the gate lets the request through
    # (here it stops at the missing optional dependency, which is expected).
    # loop:// is a local port on every OS - COM7 is legal only on win32
    # (IH-113 whitelist), and the test must not depend on the runner platform.
    monkeypatch.setenv(esp_mod.ALLOW_REAL_FLASH_ENV, "1")
    monkeypatch.setattr(esp_mod, "_ESPTOOL_AVAILABLE", False)
    with pytest.raises(ImportError, match="flash.*extra"):
        EspFlasher().flash("loop://", image)
    with pytest.raises(ImportError, match="flash.*extra"):
        EspFlasher().erase("loop://")


def test_flash_and_erase_refuse_nonlocal_ports(tmp_path, image, monkeypatch, bomb_connect):
    # IH-113: esptool opens the port with serial_for_url, which accepts network
    # URLs - socket:// must not become an outbound TCP connection, rfc2217://
    # not a remote serial session. Same whitelist as the serial transports
    # (IH-50), and NOT liftable by the real-flash opt-in: locality is policy.
    monkeypatch.setenv(esp_mod.ALLOW_REAL_FLASH_ENV, "1")
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(ValueError, match="non-local"):
        EspFlasher(on_event=jr).flash("socket://10.0.0.1:3232", image)
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(ValueError, match="non-local"):
        EspFlasher(on_event=jr).erase("rfc2217://10.0.0.1:7000")
    assert bomb_connect == []  # esptool was never reached
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["esp_denied", "esp_denied"]
    assert events[0]["port"] == "socket://10.0.0.1:3232"
    assert events[1]["op"] == "erase"


def test_whitelist_denial_precedes_the_esptool_check(tmp_path, image, monkeypatch):
    # the locality verdict must not depend on the optional dependency either
    monkeypatch.setenv(esp_mod.ALLOW_REAL_FLASH_ENV, "1")
    monkeypatch.setattr(esp_mod, "_ESPTOOL_AVAILABLE", False)
    with pytest.raises(ValueError, match="non-local"):
        EspFlasher().flash("socket://10.0.0.1:3232", image)


def test_flash_denied_with_missing_image(tmp_path, bomb_connect):
    # the gate fires before the path check: even a probe with a bogus path is
    # an unauthorized attempt and must leave an esp_denied trace (IH-29 review)
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(PermissionError):
        EspFlasher(on_event=jr).flash("COM7", tmp_path / "nope.bin")
    assert bomb_connect == []
    assert [e["kind"] for e in read_events(jpath)] == ["esp_denied"]


def test_flash_missing_image_with_open_gate_is_journaled(tmp_path, monkeypatch):
    # review N1: with the gate open, the missing-image FileNotFoundError used
    # to be the last silent raise in esp_flash
    monkeypatch.setenv(esp_mod.ALLOW_REAL_FLASH_ENV, "1")
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(FileNotFoundError):
        EspFlasher(on_event=jr).flash("COM7", tmp_path / "nope.bin")
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["esp_flash_failed"]
    assert "nope.bin" in events[0]["path"]


def test_image_info_missing_file_is_journaled(tmp_path):
    jpath = tmp_path / "j.jsonl"
    with JsonlJournal(jpath, actor="test") as jr, pytest.raises(FileNotFoundError):
        EspFlasher(on_event=jr).image_info(tmp_path / "nope.bin")
    events = read_events(jpath)
    assert [e["kind"] for e in events] == ["esp_image_info_failed"]
    assert "nope.bin" in events[0]["path"]


def test_session_flash_denial_is_journaled(tmp_path, image, monkeypatch):
    monkeypatch.delenv("IRONHARNESS_ENABLED_KINDS", raising=False)  # deterministic kinds
    jpath = tmp_path / "session.jsonl"
    s = Session(jpath, tmp_path / "sandbox", actor="test")
    with pytest.raises(PermissionError):
        s.esp_flash("COM7", str(image))
    with pytest.raises(PermissionError):
        s.esp_erase("COM7")
    s.close()
    kinds = journal_kinds(jpath)
    assert kinds.count("esp_denied") == 2
