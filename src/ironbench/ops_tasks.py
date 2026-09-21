"""Ops-golden tasks: board-operation tasks for the MCP-value A/B (IH-104).

An ops task measures how well an agent OPERATES a live board - flashing
firmware, deploying files, diagnosing a broken station - not how well it
writes firmware. Two structural differences from firmware tasks (tasks.py):

- the agent drives the board through host-side operations (its own scripts
  or MCP tools) while the runner feeds observations back over iterations
  (IH-105 arms); there is no runner-controlled stimulus loop;
- the verdict is a STATE JUDGE (ops_judge.py): after the agent claims
  success, fresh harness-owned connections inspect the actual board state.
  The agent transcript can never decide an ops task - it is the agent's
  own claim, not the board's state (checklist 7c: effect, not return text).

Tasks live under the packaged `ops/` directory (one subdir per task), which
the firmware loader deliberately does not scan.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

from ironbench.tasks import CLASS_TAGS

OPS_DIR = Path(__file__).resolve().parent / "ops"

# host-side tool families the MCP arm may see; the bare arm gets pyserial +
# esptool availability, which is the same surface expressed as code
ALLOWED_TOOLS = frozenset({"serial", "file", "esp", "mqtt"})

# asset refs must point at a declared asset
_STEP_KINDS = {
    "wipe_flash": frozenset(),
    "flash_asset": frozenset({"asset"}),
    "deploy_file": frozenset({"asset", "target"}),
    "remove_file": frozenset({"path"}),
}
_CHECK_KINDS = {
    "boot_expect": frozenset({"literals", "within_sec", "repl_echo"}),
    "device_file": frozenset({"path", "asset", "contains"}),
    "bus_scan": frozenset({"i2c_scl", "i2c_sda", "onewire_pin", "i2c_expected", "onewire_family"}),
    "inventory_match": frozenset({"i2c", "onewire", "i2c_scl", "i2c_sda", "onewire_pin"}),
    "mqtt_collect": frozenset({"host", "port", "topic", "expect_regex", "within_sec"}),
}


@dataclasses.dataclass(frozen=True)
class OpsBudget:
    max_iterations: int
    iter_timeout_sec: int


@dataclasses.dataclass(frozen=True)
class OpsAsset:
    name: str
    # exactly one of the two sources is set: a file shipped next to task.yaml,
    # or an external download pinned by sha256 (large images stay out of the
    # wheel; the pinned hash makes the download reproducible)
    path: str | None = None
    url: str | None = None
    sha256: str | None = None


@dataclasses.dataclass(frozen=True)
class OpsStep:
    kind: str
    params: dict[str, object]


@dataclasses.dataclass(frozen=True)
class OpsCheck:
    kind: str
    params: dict[str, object]


@dataclasses.dataclass(frozen=True)
class OpsTask:
    name: str
    description: str
    wall_sec: int
    budget: OpsBudget
    judge: tuple[OpsCheck, ...]
    level: int = 3
    tags: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # facts both arms receive verbatim (knowledge parity is the A/B contract:
    # equal knowledge, unequal tools - that difference is the measurement)
    dossier: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ("serial",)
    assets: tuple[OpsAsset, ...] = ()
    setup: tuple[OpsStep, ...] = ()
    restore: tuple[OpsStep, ...] = ()

    def asset(self, name: str) -> OpsAsset:
        for asset in self.assets:
            if asset.name == name:
                return asset
        raise ValueError(f"task {self.name}: undeclared asset {name!r}")


def _reject_unknown(where: str, given: dict, allowed: frozenset | set) -> None:
    unknown = sorted(set(given) - set(allowed))
    if unknown:
        raise ValueError(f"{where}: unknown keys {unknown} (allowed: {sorted(allowed)})")


def _int_in(where: str, value: object, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{where}: expected int in [{low}, {high}], got {value!r}")
    return value


def _parse_asset(name: str, spec: object) -> OpsAsset:
    if not isinstance(spec, dict):
        raise TypeError(f"asset {name!r}: expected a mapping, got {type(spec).__name__}")
    _reject_unknown(f"asset {name!r}", spec, {"path", "url", "sha256"})
    path, url, sha = spec.get("path"), spec.get("url"), spec.get("sha256")
    if bool(path) == bool(url):
        raise ValueError(f"asset {name!r}: exactly one of path/url is required")
    if url is not None:
        if not isinstance(url, str) or not url.startswith(("https://", "file://")):
            # scheme policy (https-only for real downloads, file://localhost
            # for offline fixtures) lives in ops_assets.validate_asset_url;
            # the loader checks the shape, the resolver owns the boundary
            raise ValueError(f"asset {name!r}: url must be https or file://, got {url!r}")
        if not isinstance(sha, str) or len(sha) != 64:
            raise ValueError(f"asset {name!r}: a 64-char sha256 pin is mandatory for url assets")
    elif not isinstance(path, str) or not path:
        raise ValueError(f"asset {name!r}: path must be a non-empty string")
    return OpsAsset(name=name, path=path, url=url, sha256=sha)


def _parse_steps(where: str, raw: object) -> tuple[OpsStep, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TypeError(f"{where}: expected a list of steps")
    steps: list[OpsStep] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ValueError(f"{where}[{i}]: expected a single-key mapping {{kind: params}}")
        kind, params = next(iter(entry.items()))
        if kind not in _STEP_KINDS:
            raise ValueError(f"{where}[{i}]: unknown step kind {kind!r}")
        params = params or {}
        if not isinstance(params, dict):
            raise TypeError(f"{where}[{i}]: params must be a mapping")
        _reject_unknown(f"{where}[{i}].{kind}", params, _STEP_KINDS[kind])
        missing = _STEP_KINDS[kind] - set(params)
        if missing:
            raise ValueError(f"{where}[{i}].{kind}: missing params {sorted(missing)}")
        if kind == "flash_asset" and not isinstance(params.get("asset"), str):
            raise ValueError(f"{where}[{i}].flash_asset: asset must be a name")
        if kind == "deploy_file":
            target = params.get("target")
            if not isinstance(target, str) or not target.startswith("/"):
                raise ValueError(f"{where}[{i}].deploy_file: target must be an absolute device path")
        for key in ("asset", "path"):
            if key in params and not isinstance(params[key], str):
                raise ValueError(f"{where}[{i}].{kind}: {key} must be a string")
        steps.append(OpsStep(kind=kind, params=dict(params)))
    return tuple(steps)


def _parse_checks(where: str, raw: object) -> tuple[OpsCheck, ...]:
    if raw is None or not isinstance(raw, list) or not raw:
        # the ops analog of "any output would pass": a task without state
        # checks is not an ops task, it is an opinion
        raise ValueError(f"{where}: at least one state check is mandatory")
    checks: list[OpsCheck] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ValueError(f"{where}[{i}]: expected a single-key mapping {{kind: params}}")
        kind, params = next(iter(entry.items()))
        if kind not in _CHECK_KINDS:
            raise ValueError(f"{where}[{i}]: unknown check kind {kind!r}")
        params = params or {}
        if not isinstance(params, dict):
            raise TypeError(f"{where}[{i}]: params must be a mapping")
        _reject_unknown(f"{where}[{i}].{kind}", params, _CHECK_KINDS[kind])
        if kind == "boot_expect":
            literals = params.get("literals")
            if not isinstance(literals, list) or not literals or not all(
                isinstance(x, str) and x for x in literals
            ):
                raise ValueError(f"{where}[{i}].boot_expect: literals must be a non-empty str list")
            if "within_sec" in params:
                _int_in(f"{where}[{i}].boot_expect", params["within_sec"], 5, 120)
            if not isinstance(params.get("repl_echo", False), bool):
                raise ValueError(f"{where}[{i}].boot_expect: repl_echo must be boolean")
        elif kind == "device_file":
            path = params.get("path")
            if not isinstance(path, str) or not path.startswith("/"):
                raise ValueError(f"{where}[{i}].device_file: path must be an absolute device path")
            if bool("asset" in params) == bool("contains" in params):
                raise ValueError(
                    f"{where}[{i}].device_file: exactly one of asset/contains is required"
                )
            if "contains" in params and not isinstance(params["contains"], str):
                raise ValueError(f"{where}[{i}].device_file: contains must be a regex string")
        elif kind in ("bus_scan", "inventory_match"):
            for key in ("i2c_scl", "i2c_sda", "onewire_pin"):
                if key in params:
                    _int_in(f"{where}[{i}].{kind}", params[key], 0, 60)
            if kind == "bus_scan":
                if "i2c_expected" in params:
                    expected = params["i2c_expected"]
                    if not isinstance(expected, list) or not all(
                        isinstance(x, int) and 0 <= x <= 0x7F for x in expected
                    ):
                        raise ValueError(
                            f"{where}[{i}].bus_scan: i2c_expected must be a list of 0..127 ints"
                        )
                if "onewire_family" in params:
                    _int_in(f"{where}[{i}].bus_scan", params["onewire_family"], 0, 255)
            else:
                for key in ("i2c", "onewire"):
                    if not isinstance(params.get(key, False), bool):
                        raise TypeError(f"{where}[{i}].inventory_match: {key} must be boolean")
                if not (params.get("i2c", False) or params.get("onewire", False)):
                    raise ValueError(
                        f"{where}[{i}].inventory_match: at least one of i2c/onewire is required"
                    )
        elif kind == "mqtt_collect":
            for key in ("topic", "expect_regex"):
                if not isinstance(params.get(key), str) or not params[key]:
                    raise ValueError(f"{where}[{i}].mqtt_collect: {key} must be a non-empty string")
            if "within_sec" in params:
                _int_in(f"{where}[{i}].mqtt_collect", params["within_sec"], 5, 300)
            host, port = params.get("host"), params.get("port")
            if host is not None and not isinstance(host, str):
                raise ValueError(f"{where}[{i}].mqtt_collect: host must be a string")
            if port is not None and not _is_port(port):
                raise ValueError(f"{where}[{i}].mqtt_collect: port must be an int in [1, 65535]")
            if host is None and port is None:
                # CI/offline form: the judge falls back to OPS_AB_MQTT_HOST/PORT
                pass
            elif host is None or port is None:
                raise ValueError(
                    f"{where}[{i}].mqtt_collect: host and port must be set together "
                    "(or both omitted for env fallback)"
                )
        checks.append(OpsCheck(kind=kind, params=dict(params)))
    return tuple(checks)


def _is_port(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def load_ops_task(path: Path) -> OpsTask:
    """Loads and validates one ops task YAML; unknown keys are rejected."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"{path}: expected a mapping at the top level")
    _reject_unknown(
        str(path),
        raw,
        {
            "name",
            "description",
            "wall_sec",
            "budget",
            "judge",
            "level",
            "tags",
            "notes",
            "dossier",
            "allowed_tools",
            "assets",
            "setup",
            "restore",
        },
    )
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{path}: name must be a non-empty string")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{path}: description must be a non-empty string (it is the agent's input)")
    wall_sec = _int_in(f"{path}: wall_sec", raw.get("wall_sec", 900), 60, 3600)
    level = _int_in(f"{path}: level", raw.get("level", 3), 1, 5)
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not set(tags) <= set(CLASS_TAGS):
        raise ValueError(f"{path}: tags must be a subset of {sorted(CLASS_TAGS)}")
    notes = raw.get("notes", [])
    dossier = raw.get("dossier", [])
    for key, value in (("notes", notes), ("dossier", dossier)):
        if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
            raise ValueError(f"{path}: {key} must be a non-empty string list")
    tools = raw.get("allowed_tools", ["serial"])
    if not isinstance(tools, list) or not tools or not set(tools) <= set(ALLOWED_TOOLS):
        raise ValueError(f"{path}: allowed_tools must be a non-empty subset of {sorted(ALLOWED_TOOLS)}")
    budget_raw = raw.get("budget") or {}
    if not isinstance(budget_raw, dict):
        raise TypeError(f"{path}: budget must be a mapping")
    _reject_unknown(f"{path}: budget", budget_raw, {"max_iterations", "iter_timeout_sec"})
    budget = OpsBudget(
        max_iterations=_int_in(f"{path}: budget.max_iterations", budget_raw.get("max_iterations", 8), 1, 20),
        iter_timeout_sec=_int_in(
            f"{path}: budget.iter_timeout_sec", budget_raw.get("iter_timeout_sec", 300), 10, 1200
        ),
    )
    assets_raw = raw.get("assets") or {}
    if not isinstance(assets_raw, dict):
        raise TypeError(f"{path}: assets must be a mapping")
    assets = tuple(_parse_asset(n, spec) for n, spec in assets_raw.items())
    for asset in assets:
        if asset.path is not None and not (path.parent / asset.path).is_file():
            raise ValueError(f"{path}: asset {asset.name!r} file is missing: {asset.path}")
    setup = _parse_steps(f"{path}: setup", raw.get("setup"))
    restore = _parse_steps(f"{path}: restore", raw.get("restore"))
    for step in (*setup, *restore):
        if "asset" in step.params:
            ref = step.params["asset"]
            if not any(a.name == ref for a in assets):
                raise ValueError(f"{path}: step {step.kind} references undeclared asset {ref!r}")
    judge = _parse_checks(f"{path}: judge", raw.get("judge"))
    for check in judge:
        if "asset" in check.params:
            ref = check.params["asset"]
            if not any(a.name == ref for a in assets):
                raise ValueError(f"{path}: check {check.kind} references undeclared asset {ref!r}")
    return OpsTask(
        name=name,
        description=description,
        wall_sec=wall_sec,
        budget=budget,
        judge=judge,
        level=level,
        tags=tuple(tags),
        notes=tuple(notes),
        dossier=tuple(dossier),
        allowed_tools=tuple(tools),
        assets=assets,
        setup=setup,
        restore=restore,
    )


def load_ops_tasks(directory: Path | None = None) -> list[OpsTask]:
    """All packaged ops tasks, sorted by name."""
    root = Path(directory) if directory is not None else OPS_DIR
    tasks = [load_ops_task(p) for p in sorted(root.glob("*/task.yaml"))]
    names = [t.name for t in tasks]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"duplicate ops task names: {duplicates}")
    return tasks
