"""Ops task schema + asset resolution (IH-104). The packaged goldens must
load, and every refusal path must be loud: an ops task without state checks
is an opinion, and a golden that does not match its sha256 pin must stop
the attempt instead of degrading."""

import hashlib
from pathlib import Path

import pytest

from ironbench.ops_assets import resolve_asset, validate_asset_url
from ironbench.ops_tasks import load_ops_task, load_ops_tasks

GOLDEN_METEO_SHA = hashlib.sha256(
    (Path(__file__).parents[1] / "src/ironbench/ops/ops-wipe-deploy/meteo_main.py").read_bytes()
).hexdigest()


def test_packaged_ops_tasks_load():
    tasks = {t.name: t for t in load_ops_tasks()}
    assert set(tasks) == {
        "ops-wipe-deploy",
        "ops-restore",
        "ops-bus-inventory",
        "ops-flash-verify",
        "ops-meteo-config",
        "ops-mqtt-provision",
    }
    # every task has a judge and a wall budget; the destructive ones declare
    # a restore path back to the healthy bench station
    for name, task in tasks.items():
        assert task.judge, name
        assert 60 <= task.wall_sec <= 3600, name
        assert 1 <= task.budget.max_iterations <= 20, name
    assert [s.kind for s in tasks["ops-wipe-deploy"].setup] == ["wipe_flash"]
    assert [s.kind for s in tasks["ops-wipe-deploy"].restore] == ["flash_asset", "deploy_file"]
    assert tasks["ops-restore"].judge[1].kind == "device_file"
    assert tasks["ops-flash-verify"].judge[0].params["repl_echo"] is True


def test_golden_meteo_bytes_are_stable():
    # the deployed station source is the golden: any edit here changes what
    # every flash/deploy task restores - it must be a conscious act
    src = Path(__file__).parents[1] / "src/ironbench/ops/ops-wipe-deploy/meteo_main.py"
    assert GOLDEN_METEO_SHA == "ba92bbf5ddc97d797afa356c8267118a980d60221d0a1db47baaad0be3a20440"
    assert b"METEO BOOT" in src.read_bytes()


def test_asset_may_reference_sibling_task_dir():
    tasks = {t.name: t for t in load_ops_tasks()}
    restored = tasks["ops-restore"].asset("meteo_main")
    assert restored.path == "../ops-wipe-deploy/meteo_main.py"
    task_dir = Path(__file__).parents[1] / "src/ironbench/ops/ops-restore"
    path = resolve_asset(restored, task_dir=task_dir, cache_dir=Path("."))
    assert path.name == "meteo_main.py"


def _write_task(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "task.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL = """\
name: ops-x
description: probe
wall_sec: 600
budget: {max_iterations: 2, iter_timeout_sec: 60}
judge:
  - boot_expect: {literals: [OK]}
"""


@pytest.mark.parametrize(
    "body,why",
    [
        (MINIMAL.replace("wall_sec: 600", "wall_sec: 10"), "wall below floor"),
        (MINIMAL.replace("wall_sec: 600", "wall_sec: 9999"), "wall above cap"),
        (MINIMAL + "surprise: 1\n", "unknown top key"),
        (MINIMAL.replace("  - boot_expect: {literals: [OK]}\n", ""), "no judge checks"),
        (
            MINIMAL.replace("boot_expect: {literals: [OK]}", "vibes: {strict: true}"),
            "unknown check kind",
        ),
        (
            MINIMAL.replace("boot_expect: {literals: [OK]}", "boot_expect: {literals: []}"),
            "empty literals",
        ),
        (
            MINIMAL.replace(
                "judge:\n  - boot_expect: {literals: [OK]}\n",
                "judge:\n  - inventory_match: {}\n",
            ),
            "inventory with neither bus",
        ),
        (
            MINIMAL.replace(
                "judge:\n  - boot_expect: {literals: [OK]}\n",
                "judge:\n  - device_file: {path: /main.py}\n",
            ),
            "device_file without asset/contains",
        ),
        (
            MINIMAL.replace(
                "judge:\n  - boot_expect: {literals: [OK]}\n",
                "judge:\n  - device_file: {path: main.py, contains: x}\n",
            ),
            "device_file relative path",
        ),
        (
            MINIMAL.replace(
                "judge:\n  - boot_expect: {literals: [OK]}\n",
                "judge:\n  - mqtt_collect: {topic: t, expect_regex: x, host: h}\n",
            ),
            "mqtt host without port",
        ),
        (
            MINIMAL + "setup:\n  - deploy_file: {asset: nope, target: /x}\n",
            "step references undeclared asset",
        ),
        (
            MINIMAL.replace(
                "judge:\n  - boot_expect: {literals: [OK]}\n",
                "judge:\n  - device_file: {path: /m, asset: nope}\n",
            ),
            "check references undeclared asset",
        ),
        (
            MINIMAL.replace("budget: {max_iterations: 2, iter_timeout_sec: 60}", "budget: {max_iterations: 99}"),
            "iterations above cap",
        ),
        (
            """\
name: ops-x
description: probe
wall_sec: 600
assets:
  fw: {url: "https://example.com/fw.bin"}
judge:
  - boot_expect: {literals: [OK]}
""",
            "url asset without sha pin",
        ),
        (
            """\
name: ops-x
description: probe
wall_sec: 600
assets:
  fw: {path: a.bin, url: "https://example.com/fw.bin", sha256: 0000000000000000000000000000000000000000000000000000000000000000}
judge:
  - boot_expect: {literals: [OK]}
""",
            "both path and url",
        ),
        (
            MINIMAL + "allowed_tools: [modbus]\n",
            "bad tool family",
        ),
    ],
)
def test_validation_refusals(tmp_path, body, why):
    path = _write_task(tmp_path, body)
    with pytest.raises((TypeError, ValueError), match=r"."):
        load_ops_task(path)


def test_url_download_verified_against_pin(tmp_path):
    payload = b"GOLDEN-BYTES"
    sha = hashlib.sha256(payload).hexdigest()
    source = tmp_path / "src.bin"
    source.write_bytes(payload)
    body = f"""\
name: ops-x
description: probe
wall_sec: 600
assets:
  fw: {{url: "file://localhost/{source.as_posix()}", sha256: {sha}}}
judge:
  - boot_expect: {{literals: [OK]}}
"""
    task = load_ops_task(_write_task(tmp_path / "t", body))
    cache = tmp_path / "cache"
    path = resolve_asset(task.asset("fw"), task_dir=tmp_path / "t", cache_dir=cache)
    assert path.read_bytes() == payload


def test_url_pin_mismatch_refuses(tmp_path):
    source = tmp_path / "src.bin"
    source.write_bytes(b"NOT-THE-GOLDEN")
    sha = hashlib.sha256(b"GOLDEN").hexdigest()
    body = f"""\
name: ops-x
description: probe
wall_sec: 600
assets:
  fw: {{url: "file://localhost/{source.as_posix()}", sha256: {sha}}}
judge:
  - boot_expect: {{literals: [OK]}}
"""
    task = load_ops_task(_write_task(tmp_path / "t", body))
    with pytest.raises(ValueError, match="sha256"):
        resolve_asset(task.asset("fw"), task_dir=tmp_path / "t", cache_dir=tmp_path / "cache")


def test_warm_cache_corruption_refuses(tmp_path):
    source = tmp_path / "src.bin"
    source.write_bytes(b"GOLDEN")
    sha = hashlib.sha256(b"GOLDEN").hexdigest()
    body = f"""\
name: ops-x
description: probe
wall_sec: 600
assets:
  fw: {{url: "file://localhost/{source.as_posix()}", sha256: {sha}}}
judge:
  - boot_expect: {{literals: [OK]}}
"""
    task = load_ops_task(_write_task(tmp_path / "t", body))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"fw-{sha[:16]}").write_bytes(b"CORRUPTED")
    with pytest.raises(ValueError, match="pin"):
        resolve_asset(task.asset("fw"), task_dir=tmp_path / "t", cache_dir=cache)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/fw.bin",  # loopback
        "https://169.254.169.254/latest",  # cloud metadata
        "https://10.9.9.9/fw.bin",  # private
    ],
)
def test_resolve_asset_enforces_the_ssrf_gate_before_any_io(tmp_path, url):
    # the boundary lives inside resolve_asset, not only in the helper:
    # removing the call must turn this test red (decorative-gate rule)
    task = load_ops_task(
        _write_task(
            tmp_path / "t",
            f"""\
name: ops-x
description: probe
wall_sec: 600
assets:
  fw: {{url: "{url}", sha256: {hashlib.sha256(b"x").hexdigest()}}}
judge:
  - boot_expect: {{literals: [OK]}}
""",
        )
    )
    with pytest.raises(ValueError):
        resolve_asset(task.asset("fw"), task_dir=tmp_path / "t", cache_dir=tmp_path / "cache")


@pytest.mark.parametrize(
    "url",
    [
        "http://micropython.org/fw.bin",  # plaintext
        "https://127.0.0.1/fw.bin",  # loopback
        "https://10.0.0.5/fw.bin",  # private
        "https://169.254.169.254/latest",  # cloud metadata
        "https://[fe80::1]/fw.bin",  # link-local
        "file://evil.example/fw.bin",  # remote file url
    ],
)
def test_asset_url_ssrf_boundary(url):
    with pytest.raises(ValueError):
        validate_asset_url(url)


def test_firmware_asset_pin_matches_the_live_download():
    # the two flash tasks pin the image we verified against the live board
    tasks = {t.name: t for t in load_ops_tasks()}
    fw = tasks["ops-wipe-deploy"].asset("firmware")
    assert fw.sha256 == tasks["ops-flash-verify"].asset("firmware").sha256
    assert fw.sha256 == "aa4be80ec695911ba0f13f7558e559ce540f90efbf40c23b853ca49162136b9f"
