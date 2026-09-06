"""Отчёт ironbench (2.5): агрегирует результаты solve-кампаний в JSON + HTML.

Вход — каталог кампаний (по умолчанию .ironbench/solve), в нём рекурсивно ищутся
results.jsonl из CLI solve. Метрики на пару (model, task): число попыток, решённых,
success rate (решённые/попытки) и pass@k — «решается хотя бы одной из k попыток»,
k = число попыток этой пары.
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


def build_report(solve_dir: Path) -> dict:
    """Агрегат кампании: JSON-структура + pass@k (доля пар с хотя бы одним решением)."""
    records = load_results(solve_dir)
    stats = aggregate(records)
    pass_at_k = (
        round(sum(1 for s in stats if s.passed) / len(stats), 2) if stats else 0.0
    )
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
        } for s in stats],
    }


def render_html(report: dict) -> str:
    rows = []
    for g in report["groups"]:
        cls = "pass" if g["passed"] else "fail"
        rows.append(
            f'<tr><td>{html_escape(g["model"])}</td><td>{html_escape(g["task"])}</td>'
            f'<td>{g["attempts"]}</td><td>{g["solved"]}</td>'
            f'<td class="{cls}">{g["success_rate"]:.0%}</td>'
            f"<td>{g['avg_iterations']}</td><td>{g['avg_duration']}</td></tr>"
        )
    return HTML_TEMPLATE.format(
        campaign=html_escape(report["campaign"]),
        rows="\n".join(rows),
        pass_at_k=f"{report['pass_at_k']:.0%}",
    )


def write_report(solve_dir: Path, out_dir: Path) -> tuple[Path, Path]:
    """JSON + HTML отчёта; возвращает их пути."""
    report = build_report(solve_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    html_path = out_dir / "report.html"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path
