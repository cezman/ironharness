"""Loading ironbench task descriptions from YAML (task.yaml in the task directory)."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import yaml

from io_core.faults import Fault
from ironbench.plant import PLANT_MODELS

TASK_FILE = "task.yaml"

# Task run targets; real (stage 3) is in the plan. unix = the MicroPython unix port
# in WSL2: free local runs of pure-serial tasks (see runner._run_unix);
# plant = a closed "plant + controller" loop in Python (see runner._run_plant)
TASK_TARGETS = ("wokwi", "renode", "unix", "plant", "real")

# wokwi scenario step types allowed in stimulus; extend together with wokwi-cli
STIMULUS_STEP_KEYS = frozenset(
    {"write-serial", "wait-serial", "delay", "set-control", "mqtt-publish", "mqtt-collect"}
)
# MQTT steps (the harness is the second party of the exchange): only available to
# the unix target (an mqtt_sim broker starts next to the firmware in WSL2)
MQTT_STEP_KEYS = frozenset({"mqtt-publish", "mqtt-collect"})

# Keys of the renode section in task.yaml: platform (.repl from the Renode shipset),
# firmware (.elf from the task directory or tasks/_firmware), UART peripheral name
# for the terminal
RENODE_KEYS = frozenset({"platform", "firmware", "uart"})

# The noise section: a noisy line on top of the unix target stimulus. seed is for
# determinism, faults are the same scenarios as io_core.FaultyTransport (Fault dicts)
NOISE_KEYS = frozenset({"seed", "faults"})

# The plant section (plant target): first-order plant physics + step-response
# requirements. Key meanings - in ironbench/plant.py
PLANT_KEYS = frozenset(
    {
        "model",
        "K",
        "T",
        "ambient",
        "y0",
        "dt",
        "duration",
        "u_min",
        "u_max",
        "noise_std",
        "seed",
        "setpoint",
        "requirements",
        "disturbances",
    }
)
PLANT_REQUIREMENT_KEYS = frozenset({"steady_error", "overshoot", "settle_time"})
PLANT_DISTURBANCE_KEYS = frozenset({"at", "ambient"})

# The mqtt section (unix target): the harness starts a mini mqtt_sim broker next to
# the firmware (in WSL2) and itself takes part in the exchange via mqtt-publish/
# mqtt-collect steps. Every publish received by the harness is appended to the
# serial log as "mqtt: <topic> <payload>" - so expect/fail patterns work on MQTT too.
MQTT_KEYS = frozenset({"client_id"})

# The shim section (unix target): the runner stages a harness-provided module
# (e.g. machine.py, see ironbench/shims/) next to the entry so `import machine`
# resolves to the shim. The shim logs hardware events to stdout with timestamps.
SHIM_NAMES = ("machine",)

# The events section (unix target): timing-aware scoring over shim event lines.
# pattern matches an event line; its trailing number is the event timestamp (ms).
# count_min = minimum number of matched events; period_ms = [lo, hi] bounds for
# every consecutive interval between matched events.
EVENTS_KEYS = frozenset({"pattern", "count_min", "period_ms"})

# Benchmark taxonomy: the class tag is the core skill of the task, level is the
# difficulty step 1..5. The report shows the model's per-class profile, not one number.
# debug = the firmware is given with a planted bug; the symptom report + the fixed
# behavior spec are in the description, the agent must localize and fix the bug.
CLASS_TAGS = ("io", "data", "protocol", "fsm", "control", "resilience", "debug")


@dataclasses.dataclass(frozen=True)
class Task:
    """One golden task: a Wokwi project directory + serial-output scoring criteria.

    scenario=None -> the runner generates the REPL-paste scenario from the entry
    file itself (MicroPython: the code is pasted into the REPL, see
    runner.generate_paste_scenario).
    """

    name: str
    description: str
    directory: Path
    scenario: str | None
    entry: str
    timeout_sec: int
    expect: tuple[str, ...]
    fail: tuple[str, ...]
    stimulus: tuple[dict, ...] = ()
    target: str = "wokwi"
    renode: dict = dataclasses.field(default_factory=dict)
    noise: dict = dataclasses.field(default_factory=dict)
    plant: dict = dataclasses.field(default_factory=dict)
    mqtt: dict = dataclasses.field(default_factory=dict)
    shim: str = ""
    events: tuple[dict, ...] = ()
    tags: tuple[str, ...] = ()
    level: int | None = None


def load_task(task_dir: Path) -> Task:
    """Reads task.yaml from the task directory; format errors raise ValueError with the path."""
    task_file = task_dir / TASK_FILE
    if not task_file.is_file():
        raise ValueError(f"no {TASK_FILE} in {task_dir}")
    raw = yaml.safe_load(task_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"{task_file}: expected a YAML mapping")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"{task_file}: the required field name (string) is missing")
    # `key:` with no items parses as None - an empty section, not an error
    expect = raw.get("expect") or []
    fail = raw.get("fail") or []
    if not isinstance(expect, list) or not all(isinstance(p, str) for p in expect):
        raise ValueError(f"{task_file}: expect must be a list of strings")
    if not isinstance(fail, list) or not all(isinstance(p, str) for p in fail):
        raise ValueError(f"{task_file}: fail must be a list of strings")
    scenario = raw.get("scenario")
    if scenario is not None and not isinstance(scenario, str):
        raise ValueError(f"{task_file}: scenario must be a string (path to YAML)")
    try:
        timeout_sec = int(raw.get("timeout_sec", 30))
    except (TypeError, ValueError):
        raise ValueError(f"{task_file}: timeout_sec must be an integer") from None
    stimulus = raw.get("stimulus", [])
    if not isinstance(stimulus, list) or not all(isinstance(s, dict) for s in stimulus):
        raise ValueError(f"{task_file}: stimulus must be a list of steps (mappings)")
    for step in stimulus:
        unknown = set(step) - STIMULUS_STEP_KEYS
        if unknown:
            raise ValueError(
                f"{task_file}: unknown stimulus step {sorted(unknown)} "
                f"(allowed: {sorted(STIMULUS_STEP_KEYS)})"
            )
        if "mqtt-publish" in step:
            pub = step["mqtt-publish"]
            if not isinstance(pub, dict) or not pub.get("topic") or "payload" not in pub:
                raise ValueError(
                    f"{task_file}: mqtt-publish requires topic and payload"
                )
            if set(pub) - {"topic", "payload", "retain", "qos"}:
                raise ValueError(
                    f"{task_file}: unknown mqtt-publish keys {sorted(set(pub) - {'topic', 'payload', 'retain', 'qos'})}"
                )
            if not isinstance(pub.get("retain", False), bool) or pub.get("qos", 0) not in (0, 1):
                raise ValueError(f"{task_file}: mqtt-publish.retain must be bool, qos 0 or 1")
        if "mqtt-collect" in step:
            col = step["mqtt-collect"]
            if not isinstance(col, dict) or not col.get("topic") or "count" not in col:
                raise ValueError(f"{task_file}: mqtt-collect requires topic and count")
            if set(col) - {"topic", "count", "timeout_sec"}:
                raise ValueError(
                    f"{task_file}: unknown mqtt-collect keys {sorted(set(col) - {'topic', 'count', 'timeout_sec'})}"
                )
            if (
                isinstance(col["count"], bool)
                or not isinstance(col["count"], int)
                or col["count"] < 1
            ):
                raise ValueError(f"{task_file}: mqtt-collect.count must be an integer >= 1")
            ts = col.get("timeout_sec", 10)
            if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
                raise ValueError(f"{task_file}: mqtt-collect.timeout_sec must be a number > 0")
    target = str(raw.get("target", "wokwi"))
    if target not in TASK_TARGETS:
        raise ValueError(f"{task_file}: unknown target {target!r} (allowed: {TASK_TARGETS})")
    # anti-cheat (IH-14): on the runner-anchored targets a wait-serial answer is
    # anchored to the stimulus that asked for it (a preceding write-serial or
    # mqtt-publish). A wait without any preceding trigger would false-flag
    # honest firmware output as pre-printed cheating - reject at load time.
    # (wokwi wait-serials live in the generated scenario, outside the runner.)
    if target in ("unix", "renode", "real"):
        seen_trigger = False
        for step in stimulus:
            if "write-serial" in step or "mqtt-publish" in step:
                seen_trigger = True
            if "wait-serial" in step and not seen_trigger:
                raise ValueError(
                    f"{task_file}: wait-serial must be preceded by a write-serial or "
                    "mqtt-publish step (an answer is anchored to the stimulus that "
                    "asked for it); use delay to sync with boot output"
                )
    renode = raw.get("renode", {})
    if not isinstance(renode, dict):
        raise TypeError(f"{task_file}: renode must be a mapping (platform/firmware/uart)")
    unknown_renode = set(renode) - RENODE_KEYS
    if unknown_renode:
        raise ValueError(
            f"{task_file}: unknown renode keys {sorted(unknown_renode)} "
            f"(allowed: {sorted(RENODE_KEYS)})"
        )
    if target == "renode":
        missing = {"platform", "firmware"} - set(renode)
        if missing:
            raise ValueError(
                f"{task_file}: the renode target requires platform and firmware in the renode section "
                f"(missing: {sorted(missing)})"
            )
        for key in ("platform", "firmware", "uart"):
            if key in renode and (not isinstance(renode[key], str) or not renode[key]):
                raise ValueError(f"{task_file}: renode.{key} must be a non-empty string")
    noise = raw.get("noise", {})
    if not isinstance(noise, dict):
        raise TypeError(f"{task_file}: noise must be a mapping (seed/faults)")
    unknown_noise = set(noise) - NOISE_KEYS
    if unknown_noise:
        raise ValueError(
            f"{task_file}: unknown noise keys {sorted(unknown_noise)} "
            f"(allowed: {sorted(NOISE_KEYS)})"
        )
    if "seed" in noise and (
        isinstance(noise["seed"], bool) or not isinstance(noise["seed"], int)
    ):
        raise ValueError(f"{task_file}: noise.seed must be an integer")
    faults = noise.get("faults", [])
    if not isinstance(faults, list) or not all(isinstance(f, dict) for f in faults):
        raise ValueError(f"{task_file}: noise.faults must be a list of mappings")
    for f in faults:
        # disconnect would crash the run (ConnectionLost is not caught in _run_unix),
        # the other noisy-line actions are honestly supported
        if f.get("action") not in {"drop", "corrupt", "delay"}:
            raise ValueError(
                f"{task_file}: noise.faults: action {f.get('action')!r} is not supported "
                "(allowed: drop, corrupt, delay)"
            )
    try:
        [Fault(**f) for f in faults]  # validate the fault scenarios at load time
    except (TypeError, ValueError) as e:
        raise ValueError(f"{task_file}: noise.faults: {e}") from None
    if noise and target != "unix":
        raise ValueError(f"{task_file}: noise is only supported by the unix target")
    mqtt_section = raw.get("mqtt", {})
    if mqtt_section and target != "unix":
        raise ValueError(f"{task_file}: the mqtt section is only supported by the unix target")
    if MQTT_STEP_KEYS & {k for step in stimulus for k in step}:
        if target != "unix":
            raise ValueError(f"{task_file}: mqtt steps are only supported by the unix target")
        if not isinstance(mqtt_section, dict) or not mqtt_section:
            raise ValueError(
                f"{task_file}: mqtt steps require a non-empty mqtt section (keys: {sorted(MQTT_KEYS)})"
            )
    if mqtt_section:
        if not isinstance(mqtt_section, dict):
            raise TypeError(f"{task_file}: the mqtt section must be a mapping")
        unknown_mqtt = set(mqtt_section) - MQTT_KEYS
        if unknown_mqtt:
            raise ValueError(
                f"{task_file}: unknown mqtt keys {sorted(unknown_mqtt)} "
                f"(allowed: {sorted(MQTT_KEYS)})"
            )
    shim = str(raw.get("shim", ""))
    if shim and target != "unix":
        raise ValueError(f"{task_file}: the shim is only supported by the unix target")
    if shim and shim not in SHIM_NAMES:
        raise ValueError(f"{task_file}: unknown shim {shim!r} (allowed: {sorted(SHIM_NAMES)})")
    events = raw.get("events") or []
    if events and target != "unix":
        raise ValueError(f"{task_file}: the events section is only supported by the unix target")
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        raise ValueError(f"{task_file}: events must be a list of mappings")
    for ev in events:
        unknown_ev = set(ev) - EVENTS_KEYS
        if unknown_ev:
            raise ValueError(
                f"{task_file}: unknown events keys {sorted(unknown_ev)} "
                f"(allowed: {sorted(EVENTS_KEYS)})"
            )
        if not isinstance(ev.get("pattern"), str) or not ev["pattern"]:
            raise ValueError(f"{task_file}: events.pattern must be a non-empty string")
        count_min = ev.get("count_min")
        if isinstance(count_min, bool) or not isinstance(count_min, int) or count_min < 1:
            raise ValueError(f"{task_file}: events.count_min must be an integer >= 1")
        period = ev.get("period_ms")
        if period is not None:
            if (
                not isinstance(period, list)
                or len(period) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in period)
            ):
                raise ValueError(f"{task_file}: events.period_ms must be [lo, hi] numbers")
            if not 0 < period[0] < period[1]:
                raise ValueError(f"{task_file}: events.period_ms must satisfy 0 < lo < hi")
    # compile every regex at load time: an invalid pattern must be an authoring
    # error (ValueError with the path), not a runner crash at scoring time
    for p in (*expect, *fail, *(ev["pattern"] for ev in events)):
        try:
            re.compile(p)
        except re.error as e:
            raise ValueError(f"{task_file}: invalid regex {p!r}: {e}") from e
    plant_section = raw.get("plant", {})
    if plant_section and target != "plant":
        raise ValueError(f"{task_file}: the plant section is only supported by the plant target")
    if target == "plant":
        if not isinstance(plant_section, dict):
            raise TypeError(f"{task_file}: the plant section must be a mapping")
        if not plant_section:
            raise ValueError(f"{task_file}: the plant target requires a non-empty plant section")
        unknown_plant = set(plant_section) - PLANT_KEYS
        if unknown_plant:
            raise ValueError(
                f"{task_file}: unknown plant keys {sorted(unknown_plant)} "
                f"(allowed: {sorted(PLANT_KEYS)})"
            )
        model = str(plant_section.get("model", "heater"))
        if model not in PLANT_MODELS:
            raise ValueError(
                f"{task_file}: plant.model {model!r} is not supported (allowed: {PLANT_MODELS})"
            )
        for key in ("K", "T", "duration", "setpoint"):
            if key not in plant_section:
                raise ValueError(f"{task_file}: the plant section requires the key {key}")
        for key, value in plant_section.items():
            if key in ("model", "requirements", "disturbances"):
                continue
            if key == "seed":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{task_file}: plant.seed must be an integer")
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                # loader contract - every format error is a ValueError
                raise ValueError(f"{task_file}: plant.{key} must be a number")  # noqa: TRY004
        if plant_section["K"] <= 0:
            raise ValueError(f"{task_file}: plant.K must be > 0")
        if plant_section["T"] <= 0:
            raise ValueError(f"{task_file}: plant.T must be > 0")
        dt = float(plant_section.get("dt", 0.5))
        if dt <= 0 or dt > float(plant_section["duration"]):
            raise ValueError(f"{task_file}: plant.dt must be > 0 and no greater than duration")
        if float(plant_section.get("u_min", 0.0)) >= float(plant_section.get("u_max", 1.0)):
            raise ValueError(f"{task_file}: plant.u_min must be less than u_max")
        if float(plant_section.get("noise_std", 0.0)) < 0:
            raise ValueError(f"{task_file}: plant.noise_std must be >= 0")
        requirements = plant_section.get("requirements")
        if not isinstance(requirements, dict) or not requirements:
            raise ValueError(
                f"{task_file}: plant.requirements must be a non-empty mapping of tolerances "
                f"{sorted(PLANT_REQUIREMENT_KEYS)}"
            )
        unknown_req = set(requirements) - PLANT_REQUIREMENT_KEYS
        if unknown_req:
            raise ValueError(
                f"{task_file}: unknown plant.requirements keys {sorted(unknown_req)} "
                f"(allowed: {sorted(PLANT_REQUIREMENT_KEYS)})"
            )
        for key, value in requirements.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{task_file}: plant.requirements.{key} must be a number > 0")
        disturbances = plant_section.get("disturbances", [])
        if not isinstance(disturbances, list):
            raise TypeError(f"{task_file}: plant.disturbances must be a list of mappings")
        if not all(isinstance(d, dict) for d in disturbances):
            raise TypeError(f"{task_file}: plant.disturbances must be a list of mappings")
        for d in disturbances:
            unknown_dist = set(d) - PLANT_DISTURBANCE_KEYS
            if unknown_dist:
                raise ValueError(
                    f"{task_file}: unknown disturbances event keys {sorted(unknown_dist)} "
                    f"(allowed: {sorted(PLANT_DISTURBANCE_KEYS)})"
                )
            if "at" not in d or "ambient" not in d:
                raise ValueError(f"{task_file}: a disturbances event requires at and ambient")
            if isinstance(d["at"], bool) or not isinstance(d["at"], (int, float)) or d["at"] < 0:
                raise ValueError(f"{task_file}: disturbances.at must be a number >= 0")
            if isinstance(d["ambient"], bool) or not isinstance(d["ambient"], (int, float)):
                raise ValueError(f"{task_file}: disturbances.ambient must be a number")  # noqa: TRY004
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError(f"{task_file}: tags must be a list of strings")
    if len(set(tags)) != len(tags):
        raise ValueError(f"{task_file}: tags contains duplicates")
    unknown_tags = set(tags) - set(CLASS_TAGS)
    if unknown_tags:
        raise ValueError(
            f"{task_file}: unknown tags {sorted(unknown_tags)} (allowed: {CLASS_TAGS})"
        )
    level = raw.get("level")
    if level is not None and (
        isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5
    ):
        raise ValueError(f"{task_file}: level must be an integer 1..5")
    return Task(
        name=name,
        description=str(raw.get("description", "")),
        directory=task_dir,
        scenario=scenario,
        entry=str(raw.get("entry", "main.py")),
        timeout_sec=timeout_sec,
        expect=tuple(expect),
        fail=tuple(fail),
        stimulus=tuple(stimulus),
        target=target,
        renode=dict(renode),
        noise=dict(noise),
        plant=dict(plant_section),
        mqtt=dict(mqtt_section),
        shim=shim,
        events=tuple(events),
        tags=tuple(tags),
        level=level,
    )


def load_tasks(tasks_dir: Path) -> list[Task]:
    """All tasks of the directory (subdirectories with task.yaml), ordered by name."""
    if not tasks_dir.is_dir():
        raise ValueError(f"tasks directory not found: {tasks_dir}")
    tasks = [
        load_task(d)
        for d in sorted(tasks_dir.iterdir())
        if d.is_dir() and (d / TASK_FILE).is_file()
    ]
    return sorted(tasks, key=lambda t: t.name)
