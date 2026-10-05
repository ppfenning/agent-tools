"""Models comparison core: figures per role and model id, grouped by role.

Pure: plain rows in, plain values out. The command task adapts stats.db rows
to ModelCall; nothing here reads a database, a file or the clock.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

MIN_COUNT = 20
TOO_FEW = "too few"
BUILD_ROLE = "build"


@dataclass(frozen=True)
class ModelCall:
    role: str
    model_id: str  # the id, or the alias when the call has no id
    task_id: str
    cost_usd: float
    output_tokens: int
    turns: int
    attempt: int  # 1 for the first build
    task_landed: bool
    challenger: bool


@dataclass(frozen=True)
class Figure:
    value: float | None
    count: int  # calls for per-call figures, distinct tasks for task figures


@dataclass(frozen=True)
class ModelRow:
    role: str
    model_id: str
    calls: Figure
    median_cost: Figure
    p90_cost: Figure
    output_tokens: Figure
    turns: Figure
    cost_per_landed: Figure
    first_try: Figure | None  # None outside the build role
    challenger_calls: Figure
    challenger_cost: Figure


def median_cost(costs: Sequence[float]) -> float | None:
    return statistics.median(costs) if costs else None


def p90_cost(costs: Sequence[float]) -> float | None:
    """Nearest rank: the sorted value at index ceil(0.9 * n) - 1."""
    return sorted(costs)[math.ceil(0.9 * len(costs)) - 1] if costs else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def cost_per_landed(rows: Sequence[ModelCall]) -> Figure:
    """Total cost of landed tasks' calls over landed distinct tasks."""
    landed = {r.task_id for r in rows if r.task_landed}
    total = sum(r.cost_usd for r in rows if r.task_id in landed)
    return Figure(total / len(landed) if landed else None, len(landed))


def first_try_rate(rows: Sequence[ModelCall], role: str) -> Figure | None:
    """Landed tasks with an attempt-1 call over distinct tasks; build only."""
    if role != BUILD_ROLE:
        return None
    tasks = {r.task_id for r in rows}
    first = {r.task_id for r in rows if r.task_landed and r.attempt == 1}
    return Figure(len(first) / len(tasks) if tasks else None, len(tasks))


def _cell(role: str, model_id: str, rows: Sequence[ModelCall]) -> ModelRow:
    mine = [r for r in rows if r.role == role and r.model_id == model_id]
    standard = [r for r in mine if not r.challenger]
    challenger = [r for r in mine if r.challenger]
    costs = [r.cost_usd for r in standard]
    n = len(standard)
    return ModelRow(
        role=role,
        model_id=model_id,
        calls=Figure(n, n),
        median_cost=Figure(median_cost(costs), n),
        p90_cost=Figure(p90_cost(costs), n),
        output_tokens=Figure(_mean([r.output_tokens for r in standard]), n),
        turns=Figure(_mean([r.turns for r in standard]), n),
        cost_per_landed=cost_per_landed(standard),
        first_try=first_try_rate(standard, role),
        challenger_calls=Figure(len(challenger), len(challenger)),
        challenger_cost=Figure(_mean([r.cost_usd for r in challenger]), len(challenger)),
    )


def summarise(rows: Sequence[ModelCall]) -> dict[str, dict[str, ModelRow]]:
    """Role to model id to figures. Challenger rows stay out of standard figures."""
    pairs = sorted({(r.role, r.model_id) for r in rows})
    roles = sorted({role for role, _ in pairs})
    return {role: {m: _cell(role, m, rows) for ro, m in pairs if ro == role} for role in roles}


def shown(figure: Figure, min_count: int, fmt: Callable[[float], str]) -> str:
    if figure.count < min_count:
        return TOO_FEW
    return "-" if figure.value is None else fmt(figure.value)


def _money(v: float) -> str:
    return f"{v:.2f}"


def _whole(v: float) -> str:
    return f"{v:.0f}"


def _one(v: float) -> str:
    return f"{v:.1f}"


def _percent(v: float) -> str:
    return f"{v:.0%}"


def _labelled(
    row: ModelRow,
) -> list[tuple[str, Figure | None, Callable[[float], str]]]:
    return [
        ("median cost", row.median_cost, _money),
        ("p90 cost", row.p90_cost, _money),
        ("output tokens", row.output_tokens, _whole),
        ("turns", row.turns, _one),
        ("cost per landed", row.cost_per_landed, _money),
        ("first-try", row.first_try, _percent),
        ("challenger cost", row.challenger_cost, _money),
    ]


def render_lines(summary: dict[str, dict[str, ModelRow]], min_count: int = MIN_COUNT) -> list[str]:
    def figures(row: ModelRow) -> str:
        parts = [
            f"{label} {shown(fig, min_count, fmt)} (n={fig.count})"
            for label, fig, fmt in _labelled(row)
            if fig is not None
        ]
        head = f"calls {row.calls.count}, challenger calls {row.challenger_calls.count}"
        return ", ".join([head, *parts])

    return [
        line
        for role in sorted(summary)
        for line in [
            f"role {role}",
            *[f"  {m}: {figures(summary[role][m])}" for m in sorted(summary[role])],
        ]
    ]


def _json_figure(figure: Figure | None, min_count: int, exempt: bool = False) -> dict[str, object] | None:
    if figure is None:
        return None
    too_few = figure.count < min_count and not exempt
    return {"value": TOO_FEW if too_few else figure.value, "count": figure.count}


def to_json(
    summary: dict[str, dict[str, ModelRow]], min_count: int = MIN_COUNT
) -> dict[str, dict[str, dict[str, dict[str, object] | None]]]:
    return {
        role: {
            m: {
                "calls": _json_figure(row.calls, min_count, exempt=True),
                "median_cost": _json_figure(row.median_cost, min_count),
                "p90_cost": _json_figure(row.p90_cost, min_count),
                "output_tokens": _json_figure(row.output_tokens, min_count),
                "turns": _json_figure(row.turns, min_count),
                "cost_per_landed": _json_figure(row.cost_per_landed, min_count),
                "first_try": _json_figure(row.first_try, min_count),
                "challenger_calls": _json_figure(row.challenger_calls, min_count, exempt=True),
                "challenger_cost": _json_figure(row.challenger_cost, min_count),
            }
            for m, row in models.items()
        }
        for role, models in summary.items()
    }
