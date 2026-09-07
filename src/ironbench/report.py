"""Отчёт ironbench (2.5): агрегирует результаты solve-кампаний в JSON + HTML.

Вход — каталог кампаний (по умолчанию .ironbench/solve), в нём рекурсивно ищутся
results.jsonl из CLI solve. Метрики на пару (model, task): число попыток, решённых,
success rate (решённые/попытки) и pass@k — «решается хотя бы одной из k попыток»,
k = число попыток этой пары.

Таксономия: если передана карта мета-данных задач (теги классов + уровень из
task.yaml), отчёт дополняется профилем по классам — success rate модели на классе
(io/data/protocol/fsm/control/resilience), вместо одного числа pass@k. Задача с
несколькими тегами попадает в каждый из них; задачи без мета-данных (удалены или
карта не передана) в профиль не входят.
"""

from __future__ import annotations

import dataclasses
import json
from collections import defaultdict
from html import escape as html_escape
from pathlib import Path

HTML_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>ironbench — отчёт</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 2rem; color: #222; }}
  h1 {{ font-size: 1.3rem; }}
  table {{ border-collapse: collapse; margin-top: 1rem; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 14px; text-align: left; }}
  th {{ background: #f4f4f4; }}
  .pass {{ color: #0a7d24; font-weight: 600; }}
  .fail {{ color: #b3261e; font-weight: 600; }}
  footer {{ margin-top: 1.5rem; color: #777; font-size: 0.85rem; }}
</style>
</head>
<body>
<h1>ironbench — отчёт {campaign}</h1>
<table>
<tr><th>Модель</th><th>Задача</th><th>Попыток</th><th>Решено</th><th>Success rate</th><th>Средних итераций</th><th>Среднее время, с</th></tr>
{rows}
</table>
<footer>Сгенерировано ironbench · pass@k = доля пар, решённых хотя бы одной из k попыток: {pass_at_k}</footer>
{profile_table}
</body>
</html>
"""


@dataclasses.dataclass(frozen=True)
class GroupStats:
    model: str
    task: str
    attempts: int
    solved: int
    total_iterations: int
    total_duration: float = 0.0

    @property
    def success_rate(self) -> float:
        return round(self.solved / self.attempts, 2) if self.attempts else 0.0

    @property
    def avg_iterations(self) -> float:
        return round(self.total_iterations / self.attempts, 1) if self.attempts else 0.0

    @property
    def avg_duration(self) -> float:
        """Среднее время попытки, сек — ось latency рядом с pass/fail."""
        return round(self.total_duration / self.attempts, 1) if self.attempts else 0.0

    @property
    def passed(self) -> bool:
        return self.solved > 0


def load_results(solve_dir: Path) -> list[dict]:
    """Все записи attempt_result из results.jsonl в каталоге кампаний (рекурсивно)."""
    records: list[dict] = []
    for path in sorted(solve_dir.rglob("results.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def aggregate(records: list[dict]) -> list[GroupStats]:
    """Группировка (model, task) → статистика; сортировка по модели, затем задаче."""
    groups: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0, 0, 0.0])
    for rec in records:
        stats = groups[(rec.get("model", "?"), rec.get("task", "?"))]
        stats[0] += 1
        stats[1] += 1 if rec.get("solved") else 0
        stats[2] += int(rec.get("iterations", 0))
        stats[3] += float(rec.get("duration_sec", 0))
    return [
        GroupStats(model=model, task=task, attempts=a, solved=s, total_iterations=i, total_duration=d)
        for (model, task), (a, s, i, d) in sorted(groups.items())
    ]


def build_report(solve_dir: Path, task_meta: dict | None = None) -> dict:
    """Агрегат кампании: JSON-структура + pass@k (доля пар с хотя бы одним решением).
    task_meta: имя задачи → {"tags": [...], "level": int|None} — для профиля по классам."""
    records = load_results(solve_dir)
    stats = aggregate(records)
    pass_at_k = (
        round(sum(1 for s in stats if s.passed) / len(stats), 2) if stats else 0.0
    )
    meta = task_meta or {}

    def meta_of(name: str) -> dict:
        return meta.get(name, {"tags": [], "level": None})

    return {
        "campaign": solve_dir.name,
        "models": sorted({s.model for s in stats}),
        "tasks": sorted({s.task for s in stats}),
        "pass_at_k": pass_at_k,
        "groups": [dataclasses.asdict(s) | {
            "success_rate": s.success_rate,
            "avg_iterations": s.avg_iterations,
            "avg_duration": s.avg_duration,
            "passed": s.passed,
            "tags": meta_of(s.task)["tags"],
            "level": meta_of(s.task)["level"],
        } for s in stats],
        "class_profile": class_profile(records, meta),
    }


def class_profile(records: list[dict], task_meta: dict) -> dict:
    """Профиль по классам: (модель, тег) → попытки/решённые/success rate."""
    raw: dict[str, dict[str, list[int]]] = {}
    for rec in records:
        meta = task_meta.get(rec.get("task", "?"))
        if not meta:
            continue
        for tag in meta.get("tags", ()):
            cell = raw.setdefault(rec.get("model", "?"), {}).setdefault(tag, [0, 0])
            cell[0] += 1
            cell[1] += 1 if rec.get("solved") else 0
    return {
        model: {
            tag: {
                "attempts": attempts,
                "solved": solved,
                "success_rate": round(solved / attempts, 2) if attempts else 0.0,
            }
            for tag, (attempts, solved) in sorted(cells.items())
        }
        for model, cells in sorted(raw.items())
    }


def render_html(report: dict) -> str:
    rows = []
    for g in report["groups"]:
        cls = "pass" if g["passed"] else "fail"
        meta = " · ".join(
            filter(None, (", ".join(g.get("tags", ())), f"ур. {g['level']}" if g.get("level") else ""))
        )
        rows.append(
            f'<tr><td>{html_escape(g["model"])}</td><td>{html_escape(g["task"])}'
            + (f' <small>({html_escape(meta)})</small>' if meta else "")
            + f'</td><td>{g["attempts"]}</td><td>{g["solved"]}</td>'
            f'<td class="{cls}">{g["success_rate"]:.0%}</td>'
            f"<td>{g['avg_iterations']}</td><td>{g['avg_duration']}</td></tr>"
        )
    profile_table = _render_profile_table(report.get("class_profile") or {})
    return HTML_TEMPLATE.format(
        campaign=html_escape(report["campaign"]),
        rows="\n".join(rows),
        pass_at_k=f"{report['pass_at_k']:.0%}",
        profile_table=profile_table,
    )


def _render_profile_table(profile: dict) -> str:
    """HTML таблицы «Профиль по классам»; пустая строка, если профиля нет."""
    if not profile:
        return ""
    tags = sorted({t for cells in profile.values() for t in cells})
    head = "".join(f"<th>{html_escape(t)}</th>" for t in tags)
    rows = []
    for model, cells in profile.items():
        cells_html = "".join(
            f'<td>{cells[t]["solved"]}/{cells[t]["attempts"]} ({cells[t]["success_rate"]:.0%})</td>'
            if t in cells
            else "<td>—</td>"
            for t in tags
        )
        rows.append(f"<tr><td>{html_escape(model)}</td>{cells_html}</tr>")
    return (
        "<h1>Профиль по классам (решено/попыток, успех попыток)</h1>"
        f'<table><tr><th>Модель</th>{head}</tr>{"".join(rows)}</table>'
    )


def write_report(solve_dir: Path, out_dir: Path, task_meta: dict | None = None) -> tuple[Path, Path]:
    """JSON + HTML отчёта; возвращает их пути. task_meta — карта тегов/уровней."""
    report = build_report(solve_dir, task_meta)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    html_path = out_dir / "report.html"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path
