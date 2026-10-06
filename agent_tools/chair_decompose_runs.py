"""An intake's decompose runs as DecomposeRun values, oldest first."""
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.chair_decompose_streak import DecomposeRun
from agent_tools.chair_facts import run_initiative
from agent_tools.chair_read_exits import exit_rows, row_exited

REFUSAL_MARKER = "approved but not executed"


def refusal_line(log: str) -> str | None:
    return next((line for line in log.splitlines() if REFUSAL_MARKER in line), None)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_run(record: Any) -> DecomposeRun:
    """`task_items` is the count of task items the store holds for the initiative; None means unknown.

    Only a count of zero reads as a run that wrote nothing: no run can have created an item the store lacks.
    Unknown, or an unreadable record, reads as wrote_items True, so it never joins a streak or blocks an intake."""
    get = record.get if isinstance(record, Mapping) else (lambda key: None)
    run_id, log, items = get("run_id"), get("log"), get("task_items")
    readable = isinstance(run_id, str) and (log is None or isinstance(log, str)) and (items is None or _is_count(items))
    if not readable:
        return DecomposeRun(run_id=run_id if isinstance(run_id, str) else "", wrote_items=True, refusal_line=None)
    return DecomposeRun(run_id=run_id, wrote_items=items != 0, refusal_line=refusal_line(log or ""))


def select_runs(rows: Sequence[Mapping[str, Any]], intake_id: str) -> list[Mapping[str, Any]]:
    """The `runs` rows whose id is `<intake_id>-<n>`, oldest `launched_at` first."""
    own = (r for r in rows if run_initiative(str(r.get("run_id") or "")) == intake_id)
    return sorted(own, key=lambda r: str(r.get("launched_at") or ""))


def _log(runs_dir: Path, run_id: str) -> str | None:
    try:
        return (runs_dir / f"{run_id}.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _task_items(runs_dir: Path, initiative: str) -> int | None:
    """None when the harness python is missing, since `run_store.work_items` then reads as empty."""
    if run_store._harness_python() is None:
        return None
    return sum(1 for row in run_store.work_items(runs_dir, initiative) if row.get("kind", "task") == "task")


def read_decompose_runs(runs_dir: Path, intake_id: str) -> list[DecomposeRun]:
    """Edge. A run that has not exited gets `task_items` None; the intake id is the `chair_exec.decompose_id` value."""
    items = _task_items(runs_dir, intake_id)
    return [
        parse_run(
            {
                "run_id": str(row["run_id"]),
                "log": _log(runs_dir, str(row["run_id"])),
                "task_items": items if row_exited(row) else None,
            }
        )
        for row in select_runs(exit_rows(runs_dir), intake_id)
    ]
