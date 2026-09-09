"""Plant target: a closed "plant + controller" loop in pure Python, no simulators.

The plant is a first-order lag (heater/rc/motor - one physics, different
meanings): dy/dt = (K*u - (y - ambient)) / T. The discretization is exact
(exponential), so the dt step size does not affect plant stability.

Controller contract: the entry file defines control(t, y, setpoint) -> float.
The worker calls it every dt simulation seconds (t - time, y - measurement,
setpoint - setpoint), clamps the return value to [u_min, u_max] (the actuator is
the source of integral windup) and feeds it to the plant. Scoring uses the
step-response metrics from task.yaml (overshoot/settle_time/steady_error), not regex.

The controller runs in a separate process (`python -m ironbench.plant`): a
hung/crashed controller is killed on the wall timeout and does not take the
runner down. The worker reports result.json + a text log (trajectory + summary
block). The log is the plant target's "serial": the agent sees its tail as
feedback, so metrics and errors are written at the end. The plant parameters
K/T never appear in the log: in system-id style tasks the agent must estimate
them itself.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import math
import runpy
import sys
import traceback

# Meaningful names for the same first-order physics
PLANT_MODELS = ("heater", "rc", "motor")

# Trajectory line in the log: t, setpoint, y (true), u (after clamping)
LOG_HEADER = "# t,setpoint,y,u"
MAX_LOG_ROWS = 150
MAX_STDOUT_LINES = 30

# Settling band when requirements has no steady_error: 2% of the travel span
DEFAULT_SETTLE_SPAN_FRACTION = 0.02


class _DeterministicNoise:
    """Counter-based deterministic noise: "seed:k" -> sha256 -> [0,1) -> gaussian
    (Box-Muller, carrying the second value over). Not cryptography: run
    reproducibility at a fixed seed is the point (same idea as io_core/faults.py).
    """

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._k = 0
        self._carry: float | None = None

    def _uniform(self) -> float:
        digest = hashlib.sha256(f"{self._seed}:{self._k}".encode()).digest()
        self._k += 1
        return int.from_bytes(digest[:8], "big") / 2**64

    def gauss(self, std: float) -> float:
        if std <= 0:
            return 0.0
        if self._carry is not None:
            value = self._carry
            self._carry = None
            return std * value
        u1 = max(self._uniform(), 1e-18)
        u2 = self._uniform()
        r = math.sqrt(-2.0 * math.log(u1))
        self._carry = r * math.sin(2 * math.pi * u2)
        return std * r * math.cos(2 * math.pi * u2)


@dataclasses.dataclass(frozen=True)
class PlantSpec:
    """Plant and run parameters from the plant section of task.yaml."""

    model: str
    K: float
    T: float
    ambient: float
    y0: float
    dt: float
    duration: float
    u_min: float
    u_max: float
    noise_std: float
    seed: int
    setpoint: float
    requirements: dict
    disturbances: tuple[tuple[float, float], ...] = ()

    @classmethod
    def from_section(cls, section: dict) -> PlantSpec:
        """The schema (types/keys) was already validated by tasks.load_task; this is
        just the conversion."""
        return cls(
            model=str(section.get("model", "heater")),
            K=float(section["K"]),
            T=float(section["T"]),
            ambient=float(section.get("ambient", 20.0)),
            y0=float(section.get("y0", section.get("ambient", 20.0))),
            dt=float(section.get("dt", 0.5)),
            duration=float(section["duration"]),
            u_min=float(section.get("u_min", 0.0)),
            u_max=float(section.get("u_max", 1.0)),
            noise_std=float(section.get("noise_std", 0.0)),
            seed=int(section.get("seed", 0)),
            setpoint=float(section["setpoint"]),
            requirements=dict(section.get("requirements") or {}),
            disturbances=tuple(
                (float(d["at"]), float(d["ambient"])) for d in section.get("disturbances", ())
            ),
        )

    def step(self, y: float, u: float, ambient: float, dt: float) -> float:
        """Exact first-order discretization: y never overshoots even for a large dt."""
        target = ambient + self.K * u
        return target + (y - target) * math.exp(-dt / self.T)


def run_closed_loop(
    control, spec: PlantSpec
) -> tuple[list[tuple[float, float, float, float]], str | None]:
    """Run: control(t, y_meas, setpoint) -> clamp to [u_min, u_max] -> plant.
    Returns (rows of (t, setpoint, y_true, u), error|None). Metrics are computed
    against the true y: sensor noise makes control harder but does not smear the score."""
    noise = _DeterministicNoise(spec.seed)
    y = spec.y0
    rows: list[tuple[float, float, float, float]] = []
    n = max(1, round(spec.duration / spec.dt))
    events = sorted(spec.disturbances)
    ei = 0
    ambient = spec.ambient
    for k in range(n):
        t = k * spec.dt
        while ei < len(events) and t >= events[ei][0] - 1e-9:
            ambient = events[ei][1]
            ei += 1
        y_meas = y + noise.gauss(spec.noise_std)
        try:
            raw = control(t, y_meas, spec.setpoint)
        except (Exception, SystemExit):  # noqa: BLE001 - controller code is foreign: any outcome (incl. sys.exit) = a run result
            return rows, f"controller crashed at t={t:g} s:\n{traceback.format_exc()}"
        # bool is an int: allow it explicitly, otherwise clamp(True) silently yields 1.0
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return rows, (
                f"controller returned a non-number ({raw!r}) at t={t:g} s - "
                "control must return a float"
            )
        u = min(max(float(raw), spec.u_min), spec.u_max)
        rows.append((t, spec.setpoint, y, u))
        y = spec.step(y, u, ambient, spec.dt)
    return rows, None


def compute_metrics(rows, spec: PlantSpec) -> dict | None:
    """Step-response metrics against the true y.

    settle_time - the first moment after which y never leaves the +/-band
    (band = requirements.steady_error, else 2% of the travel span);
    inf - y never entered the band."""
    if not rows:
        return None
    ys = [r[2] for r in rows]
    span = abs(spec.setpoint - spec.y0)
    if span > 1e-12:
        dev = (max(ys) - spec.setpoint) if spec.setpoint >= spec.y0 else (spec.setpoint - min(ys))
        overshoot_pct = max(0.0, dev / span * 100.0)
    else:
        overshoot_pct = 0.0
    tail = ys[-max(1, len(ys) // 10) :]
    steady_error = abs(sum(tail) / len(tail) - spec.setpoint)
    band = float(spec.requirements.get("steady_error", DEFAULT_SETTLE_SPAN_FRACTION * span))
    j = len(ys) - 1
    while j >= 0 and abs(ys[j] - spec.setpoint) <= band:
        j -= 1
    settle_time = rows[j + 1][0] if j + 1 < len(ys) else math.inf
    return {
        "overshoot_pct": overshoot_pct,
        "steady_error": steady_error,
        "settle_time": settle_time,
        "final_y": ys[-1],
    }


def check_requirements(metrics: dict, requirements: dict) -> list[str]:
    """Unmet requirements as human-readable strings - they land in
    TaskResult.missed and in the agent feedback (actual vs tolerance)."""
    missed: list[str] = []
    if "overshoot" in requirements and metrics["overshoot_pct"] > requirements["overshoot"] + 1e-9:
        missed.append(
            f"overshoot: {metrics['overshoot_pct']:.1f}% > allowed {requirements['overshoot']:g}%"
        )
    if (
        "steady_error" in requirements
        and metrics["steady_error"] > requirements["steady_error"] + 1e-9
    ):
        missed.append(
            f"steady_error: {metrics['steady_error']:.2f} > allowed {requirements['steady_error']:g}"
        )
    if "settle_time" in requirements:
        st = metrics["settle_time"]
        if st == math.inf:
            band = requirements.get("steady_error")
            missed.append(
                f"settle_time: never settled (band +-{band:g} never reached)"
                if band is not None
                else "settle_time: never settled"
            )
        elif st > requirements["settle_time"] + 1e-9:
            missed.append(f"settle_time: {st:.1f} s > allowed {requirements['settle_time']:g} s")
    return missed


def _fmt_settle(v: float) -> str:
    return "never" if v == math.inf else f"{v:.1f} s"


def render_log(
    spec: PlantSpec,
    rows,
    metrics: dict | None,
    missed: list[str],
    error: str | None,
    controller_stdout: str,
) -> str:
    """Text log of the run. K/T are deliberately not printed (system-id); the
    summary goes to the tail because the agent is shown the last lines of the log."""
    parts = [
        (
            f"# plant {spec.model}: setpoint={spec.setpoint:g} u=[{spec.u_min:g}, {spec.u_max:g}] "
            f"dt={spec.dt:g} duration={spec.duration:g} noise={spec.noise_std:g}"
        ),
    ]
    stdout_tail = controller_stdout.strip().splitlines()[-MAX_STDOUT_LINES:]
    if stdout_tail:
        parts.append("# controller stdout (tail):")
        parts.extend(stdout_tail)
    if rows:
        stride = max(1, len(rows) // MAX_LOG_ROWS)
        parts.append(LOG_HEADER)
        parts.extend(f"{t:g},{r:g},{y:.3f},{u:.4f}" for t, r, y, u in rows[::stride])
    parts.append("# --- summary ---")
    if metrics:
        parts.append(
            f"# metrics: overshoot={metrics['overshoot_pct']:.1f}% "
            f"steady_error={metrics['steady_error']:.2f} "
            f"settle_time={_fmt_settle(metrics['settle_time'])} final_y={metrics['final_y']:.2f}"
        )
    parts.extend(f"# not met: {m}" for m in missed)
    if error:
        parts.append(f"# error: {error}")
    return "\n".join(parts) + "\n"


def load_control(entry):
    """Executes the entry as a script (runpy) and extracts control(t, y, setpoint);
    (None, error) on failure. Controller code is the subject of the run: it is
    executed in the worker's separate process under the runner's wall timeout."""
    try:
        namespace = runpy.run_path(str(entry), run_name="controller")
    except (Exception, SystemExit):  # noqa: BLE001 - controller code is foreign: any outcome = a run result
        return None, f"entry file failed to run:\n{traceback.format_exc()}"
    control = namespace.get("control")
    if not callable(control):
        return None, "entry file has no control(t, y, setpoint) -> float function"
    return control, None


def worker_main(argv=None) -> int:
    """Worker entry point: the run report goes to result.json, the trajectory to
    the log; the return code is 0 even when the controller crashed (a controller
    error is a run result, not a worker crash). The one thing the worker cannot
    do is write anything when the controller kills the process outright
    (os._exit during import): a missing result.json is a failure by contract,
    enforced by the runner. result.json is stamped with --run-id: the runner
    scores only a report it itself requested, so a stale file at the result
    path is rejected, not used."""
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(
        prog="python -m ironbench.plant", description="plant target worker"
    )
    ap.add_argument("--entry", required=True, type=Path)
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--result", required=True, type=Path)
    ap.add_argument(
        "--run-id",
        default="",
        help="opaque id of this run; the runner rejects a result.json with a foreign run_id",
    )
    args = ap.parse_args(argv)

    try:
        spec = PlantSpec.from_section(json.loads(args.spec.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - a broken spec is an environment problem, but the report must still happen
        error = "failed to prepare task: plant spec is unreadable"
        args.log.write_text(f"# error: {error}\n", encoding="utf-8")
        args.result.write_text(
            json.dumps(
                {
                    "error": error,
                    "metrics": None,
                    "missed": [],
                    "steps": 0,
                    "run_id": args.run_id,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return 0
    control, error = load_control(args.entry)

    rows: list = []
    metrics = None
    missed: list[str] = []
    stdout_capture = io.StringIO()
    if error is None:
        with contextlib.redirect_stdout(stdout_capture):
            rows, error = run_closed_loop(control, spec)
        metrics = compute_metrics(rows, spec)
        if metrics is not None:
            missed = check_requirements(metrics, spec.requirements)

    args.log.write_text(
        render_log(spec, rows, metrics, missed, error, stdout_capture.getvalue()),
        encoding="utf-8",
    )
    report = {
        "error": error,
        "metrics": metrics,
        "missed": missed,
        "steps": len(rows),
        "run_id": args.run_id,
    }
    args.result.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(worker_main())
