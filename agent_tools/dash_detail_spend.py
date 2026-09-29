"""Data behind `cox dash --detail spend`: today's and this week's spend broken down by
initiative, role and model, the costliest tasks, and a projection to the weekly hard stop.

Cost totals come from `run_store.cost_since`, exactly as the loop's own accounting reads
them. The breakdown rows come from flattening `run_store.usages`, each call keeping the
fields the loop already carries (`role`, `model`, `task_id`, `cost_usd`), joined to
`run_store.work_items` for `initiative`. The projection reuses `usage_meter`'s own
percentage-of-quota accounting (`as_window`, `resets_at`) rather than a dollar ceiling,
so it answers the same question the loop's weekly hard stop asks.

`total_usd` (from `cost_since`, straight off `node_calls`) and the breakdown (from
`usages`, which reads a run's `.usage.json` file instead of its `node_calls` rows
whenever one exists) are two cuts of the same spend, not one figure derived from the
other, and they need not agree: a corrected usage file, or a live run `cost_since`
counts that `usages` does not, can move one without the other. Each window therefore
also reports `attributed_usd` (the sum of the rows the breakdown is built from) and
`unattributed_usd` (`total_usd` minus `attributed_usd`, `None` when there is no total),
so a reader of the breakdown can see whether it actually accounts for the headline
figure instead of assuming it.

`_totals_by` sums every row's `cost_usd` at full precision before rounding: rounding a
running total on each addition would drop any row under half a cent (it rounds to
nothing before the next row can add to it), so a category built entirely of cheap calls
could show $0.00. Each total is rounded exactly once, to the cent, only when it is
written into the dict this module returns.

`group_spend` and `project_to_hard_stop` are the pure core: plain data in, plain data
out, no clock and no filesystem. `_rows_since` and `build` are the thin edge.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_tools import run_store, usage_meter, usage_window

__all__ = ["build", "group_spend", "project_to_hard_stop"]

TOP_N_DEFAULT = 5
_WEEKLY_SPAN = timedelta(days=7)


def _initiative_map(runs_dir: Path) -> dict[str, str | None]:
    """Edge lookup table: `task_id -> initiative` from `work_items`."""
    return {row.get("task_id"): row.get("initiative") for row in run_store.work_items(runs_dir)}


def _calls_of(usage: Any) -> list[dict[str, Any]]:
    """The `calls` list of one `run_store.usages` entry; empty when the shape is not one."""
    calls = usage.get("calls") if isinstance(usage, dict) else None
    return calls if isinstance(calls, list) else []


def _rows_since(runs_dir: Path, since: str) -> list[dict[str, Any]]:
    """Edge. Every call from `run_store.usages(runs_dir)` with `ts >= since`, kept to
    `initiative`, `role`, `model`, `task_id`, `cost_usd`. `initiative` is `None` when
    the call's `task_id` has no matching `work_items` row."""
    initiatives = _initiative_map(runs_dir)
    return [
        {
            "initiative": initiatives.get(call.get("task_id")),
            "role": call.get("role"),
            "model": call.get("model"),
            "task_id": call.get("task_id"),
            "cost_usd": float(call.get("cost_usd") or 0.0),
        }
        for usage in run_store.usages(runs_dir).values()
        for call in _calls_of(usage)
        if isinstance(call.get("ts"), str) and call["ts"] >= since
    ]


def _totals_by(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    """Pure: summed `cost_usd` per distinct value of `key`. Every row's `cost_usd` is
    added to a full-precision running total; each total is rounded to the cent exactly
    once, when it is copied into the returned dict, never on any intermediate addition."""
    raw: dict[str, float] = {}
    for row in rows:
        name = str(row.get(key))
        raw[name] = raw.get(name, 0.0) + float(row.get("cost_usd") or 0.0)
    return {name: round(total, 2) for name, total in raw.items()}


def group_spend(rows: list[dict[str, Any]], top_n: int = TOP_N_DEFAULT) -> dict[str, Any]:
    """Pure. `rows` is a plain list of `{initiative, role, model, task_id, cost_usd}`.
    Returns `attributed_usd` (the summed `cost_usd` of exactly these rows, so a caller
    can check the breakdown against a total from elsewhere rather than assume it),
    totals by initiative, role and model, and the `top_n` costliest tasks (summed per
    `task_id` at full precision, rounded once, cost descending, `task_id` ascending to
    break a tie)."""
    by_task = _totals_by(rows, "task_id")
    top_tasks = sorted(by_task.items(), key=lambda pair: (-pair[1], pair[0]))[:top_n]
    return {
        "attributed_usd": round(sum(float(row.get("cost_usd") or 0.0) for row in rows), 2),
        "by_initiative": _totals_by(rows, "initiative"),
        "by_role": _totals_by(rows, "role"),
        "by_model": _totals_by(rows, "model"),
        "top_tasks": [{"task_id": task_id, "cost_usd": cost_usd} for task_id, cost_usd in top_tasks],
    }


def project_to_hard_stop(
    used_pct: float, ceiling_pct: float, burn_pct_per_hour: float, hours_until_reset: float,
) -> dict[str, Any]:
    """Pure. Remaining quota and the hours left at the current burn rate before it is
    spent, against `hours_until_reset`. A burn rate at or below zero never divides:
    the quota then never runs out on its own, so exhaustion reads as unreachable."""
    remaining_pct = ceiling_pct - used_pct
    hours_to_exhaustion = remaining_pct / burn_pct_per_hour if burn_pct_per_hour > 0 else float("inf")
    return {
        "remaining_pct": remaining_pct,
        "hours_to_exhaustion": hours_to_exhaustion,
        "hits_before_reset": hours_to_exhaustion < hours_until_reset,
    }


def _midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _unattributed(total: float | None, attributed: float) -> float | None:
    """Pure. `total` minus `attributed`, rounded to the cent; `None` when there is no total to compare."""
    return None if total is None else round(total - attributed, 2)


def _spend_window(runs_dir: Path, since: datetime, top_n: int) -> dict[str, Any]:
    """Edge. One window's total (via `cost_since`, as the loop's own accounting reads it)
    next to its grouping and costliest tasks (via the pure `group_spend`, built from
    `usages`). The two are independent reads and need not agree, so `unattributed_usd`
    reports the gap rather than letting the breakdown imply it accounts for the total."""
    since_text = since.isoformat()
    total = run_store.cost_since(runs_dir, since_text)
    grouped = group_spend(_rows_since(runs_dir, since_text), top_n)
    return {
        "total_usd": total,
        "unattributed_usd": _unattributed(total, grouped["attributed_usd"]),
        "since": since_text,
        **grouped,
    }


def _projection(now: datetime) -> dict[str, Any] | None:
    """Edge. The weekly-hard-stop projection from `usage_meter`'s own quota accounting;
    `None` when no rate-limit reading is available to project from."""
    meter = usage_meter.read()
    if meter is None:
        return None
    entry = meter.seven_day
    window = usage_meter.as_window(entry, now, _WEEKLY_SPAN)
    hours_until_reset = (entry.resets_at - now) / timedelta(hours=1)
    return {
        "resets_at": entry.resets_at.isoformat(),
        "ceiling_pct": window.ceiling_usd,
        "used_pct": window.spent_usd,
        "burn_pct_per_hour": window.burn_usd_per_hour,
        **project_to_hard_stop(window.spent_usd, window.ceiling_usd, window.burn_usd_per_hour, hours_until_reset),
    }


def build(runs_dir: Path, now: str) -> dict[str, Any]:
    """Edge. The data behind `cox dash --detail spend`: today's and this week's spend,
    each broken down by initiative, role and model with the costliest tasks, and a
    projection to the weekly hard stop."""
    now_dt = datetime.fromisoformat(now)
    since_week = usage_window.weekly_window_start(now_dt, reset=None)
    return {
        "today": _spend_window(runs_dir, _midnight(now_dt), TOP_N_DEFAULT),
        "week": _spend_window(runs_dir, since_week, TOP_N_DEFAULT),
        "projection": _projection(now_dt),
    }
