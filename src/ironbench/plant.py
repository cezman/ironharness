"""Мишень plant: закрытая петля «объект + регулятор» чисто в Python, без симуляторов.

Объект — апериодическое звено первого порядка (heater/rc/motor — одна физика, разные
смысловые нагрузки): dy/dt = (K·u - (y - ambient)) / T. Дискретизация точная
(экспонента), поэтому шаг dt не влияет на устойчивость объекта.

Контракт контроллера: entry-файл определяет control(t, y, setpoint) -> float.
Воркер вызывает его каждые dt симуляционных секунд (t — время, y — измерение,
setpoint — уставка), зажимает возврат в [u_min, u_max] (актюатор — источник
интегрального насыщения) и подставляет в объект. Оценка — метрики переходной
характеристики из task.yaml (overshoot/settle_time/steady_error), не regex.

Контроллер исполняется отдельным процессом (`python -m ironbench.plant`):
зависший/упавший контроллер гасится по wall-таймауту и не уносит раннер. Отчёт
воркера — result.json + текстовый лог (траектория + итоговый блок). Лог — это
«serial» plant-мишени: его хвост видит агент как фидбек, поэтому метрики и
ошибки пишутся в конце. Параметры объекта K/T в лог не попадают: в задачах
вида system-id агент обязан оценить их сам.
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

# Смысловые имена одной физики первого порядка
PLANT_MODELS = ("heater", "rc", "motor")

# Строка траектории в логе: t, setpoint, y (истинное), u (после зажима)
LOG_HEADER = "# t,setpoint,y,u"
MAX_LOG_ROWS = 150
MAX_STDOUT_LINES = 30

# Полоса установления, когда в requirements нет steady_error: 2% размаха хода
DEFAULT_SETTLE_SPAN_FRACTION = 0.02


class _DeterministicNoise:
    """Счётчикный детерминированный шум: "seed:k" → sha256 → [0,1) → гаусс
    (Бокс–Мюллер с переносом второго значения). Не криптография: воспроизводимость
    прогона при фиксированном seed и есть цель (та же идея, что в io_core/faults.py).
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
    """Параметры объекта и прогона из секции plant в task.yaml."""

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
        """Схему (типы/ключи) уже проверил tasks.load_task; здесь — преобразование."""
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
        """Точная дискретизация первого порядка: y не «улетает» при крупном dt."""
        target = ambient + self.K * u
        return target + (y - target) * math.exp(-dt / self.T)


def run_closed_loop(
    control, spec: PlantSpec
) -> tuple[list[tuple[float, float, float, float]], str | None]:
    """Прогон: control(t, y_meas, setpoint) → зажим в [u_min, u_max] → объект.
    Возвращает (строки (t, setpoint, y_истинное, u), ошибка|None). Метрики считаются
    по истинному y: шум датчика усложняет управление, но не «плавит» оценку."""
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
        except (Exception, SystemExit):  # noqa: BLE001 — код контроллера чужой: любой исход (включая sys.exit) = результат прогона
            return rows, f"контроллер упал на t={t:g} c:\n{traceback.format_exc()}"
        # bool — это int: разрешаем явно, иначе clamp(True) тихо даст 1.0
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return rows, (
                f"контроллер вернул не-число ({raw!r}) на t={t:g} c — "
                "control обязан возвращать float"
            )
        u = min(max(float(raw), spec.u_min), spec.u_max)
        rows.append((t, spec.setpoint, y, u))
        y = spec.step(y, u, ambient, spec.dt)
    return rows, None


def compute_metrics(rows, spec: PlantSpec) -> dict | None:
    """Метрики переходной характеристики по истинному y.

    settle_time — первый момент, после которого y уже не покидает полосу
    ±band (band = requirements.steady_error, иначе 2% размаха хода);
    inf — в полосу не вошла никогда."""
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
    """Невыполненные требования человекочитаемыми строками — они попадают в
    TaskResult.missed и в фидбек агента (факт против допуска)."""
    missed: list[str] = []
    if "overshoot" in requirements and metrics["overshoot_pct"] > requirements["overshoot"] + 1e-9:
        missed.append(
            f"overshoot: {metrics['overshoot_pct']:.1f}% > допуска {requirements['overshoot']:g}%"
        )
    if (
        "steady_error" in requirements
        and metrics["steady_error"] > requirements["steady_error"] + 1e-9
    ):
        missed.append(
            f"steady_error: {metrics['steady_error']:.2f} > допуска {requirements['steady_error']:g}"
        )
    if "settle_time" in requirements:
        st = metrics["settle_time"]
        if st == math.inf:
            band = requirements.get("steady_error")
            missed.append(
                f"settle_time: не установилось (полоса ±{band:g} не достигнута)"
                if band is not None
                else "settle_time: не установилось"
            )
        elif st > requirements["settle_time"] + 1e-9:
            missed.append(f"settle_time: {st:.1f} c > допуска {requirements['settle_time']:g} c")
    return missed


def _fmt_settle(v: float) -> str:
    return "никогда" if v == math.inf else f"{v:.1f} c"


def render_log(
    spec: PlantSpec,
    rows,
    metrics: dict | None,
    missed: list[str],
    error: str | None,
    controller_stdout: str,
) -> str:
    """Текстовый лог прогона. K/T сознательно не печатаются (system-id); итог —
    в хвосте, потому что агенту показывают последние строки лога."""
    parts = [
        (
            f"# plant {spec.model}: setpoint={spec.setpoint:g} u=[{spec.u_min:g}, {spec.u_max:g}] "
            f"dt={spec.dt:g} duration={spec.duration:g} noise={spec.noise_std:g}"
        ),
    ]
    stdout_tail = controller_stdout.strip().splitlines()[-MAX_STDOUT_LINES:]
    if stdout_tail:
        parts.append("# stdout контроллера (хвост):")
        parts.extend(stdout_tail)
    if rows:
        stride = max(1, len(rows) // MAX_LOG_ROWS)
        parts.append(LOG_HEADER)
        parts.extend(f"{t:g},{r:g},{y:.3f},{u:.4f}" for t, r, y, u in rows[::stride])
    parts.append("# --- итог ---")
    if metrics:
        parts.append(
            f"# метрики: overshoot={metrics['overshoot_pct']:.1f}% "
            f"steady_error={metrics['steady_error']:.2f} "
            f"settle_time={_fmt_settle(metrics['settle_time'])} final_y={metrics['final_y']:.2f}"
        )
    parts.extend(f"# не выполнено: {m}" for m in missed)
    if error:
        parts.append(f"# ошибка: {error}")
    return "\n".join(parts) + "\n"


def load_control(entry):
    """Исполняет entry как скрипт (runpy) и достаёт control(t, y, setpoint);
    (None, ошибка) при сбое. Код контроллера — предмет прогона, исполняется
    в отдельном процессе воркера под wall-таймаутом раннера."""
    try:
        namespace = runpy.run_path(str(entry), run_name="controller")
    except (Exception, SystemExit):  # noqa: BLE001 — код контроллера чужой: любой исход = результат прогона
        return None, f"entry-файл не исполняется:\n{traceback.format_exc()}"
    control = namespace.get("control")
    if not callable(control):
        return None, "в entry-файле нет функции control(t, y, setpoint) -> float"
    return control, None


def worker_main(argv=None) -> int:
    """Точка входа воркера: всегда пишет result.json; код возврата 0, даже когда
    контроллер упал (ошибка контроллера — это результат прогона, не крах воркера)."""
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(
        prog="python -m ironbench.plant", description="воркер мишени plant"
    )
    ap.add_argument("--entry", required=True, type=Path)
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--result", required=True, type=Path)
    args = ap.parse_args(argv)

    try:
        spec = PlantSpec.from_section(json.loads(args.spec.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 — битая спецификация = проблема среды, но отчёт обязан состояться
        error = "не удалось подготовить задачу: спецификация plant не читается"
        args.log.write_text(f"# ошибка: {error}\n", encoding="utf-8")
        args.result.write_text(
            json.dumps({"error": error, "metrics": None, "missed": [], "steps": 0}, ensure_ascii=False),
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
    report = {"error": error, "metrics": metrics, "missed": missed, "steps": len(rows)}
    args.result.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(worker_main())
