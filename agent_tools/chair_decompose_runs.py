"""An intake's decompose runs as DecomposeRun values, oldest first."""
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.chair_decompose_streak import DecomposeRun
from agent_tools.chair_facts import run_initiative
from agent_tools.chair_read_exits import exit_rows, row_exited

REFUSAL_MARKERS = ("approved but not executed", "initiative-decompose failed:")
LINT_REFUSAL_MARKER = "initiative-decompose failed:"  # the reach lint's refusal; such a run wrote nothing


def refusal_line(log: str) -> str | None:
    return next((line for line in log.splitlines() if any(m in line for m in REFUSAL_MARKERS)), None)


def log_wrote_items(run_id: str, log: str) -> bool | None:
    """A lint refusal means False, a `recorded <run_id>:` line means its proposal count is above zero, else None.

    None means the log cannot say (an older or unreadable log), so the caller falls back to the store count."""
    if LINT_REFUSAL_MARKER in log:
        return False
    recorded = re.search(
        rf"recorded {re.escape(run_id)}: \d+ auto-applied, \d+ gated decision\(s\), (\d+) proposal\(s\)", log
    )
    return int(recorded.group(1)) > 0 if recorded else None


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_run(record: Any) -> DecomposeRun:
    """The run's own log decides wrote_items; `task_items`, the initiative's task count (None means unknown), is the fallback.

    The fallback reads zero as a run that wrote nothing, since no run can have created an item the store lacks.
    Unknown, or an unreadable record, reads as wrote_items True, so it never joins a streak or blocks an intake."""
    get = record.get if isinstance(record, Mapping) else (lambda key: None)
    run_id, log, items = get("run_id"), get("log"), get("task_items")
    readable = isinstance(run_id, str) and (log is None or isinstance(log, str)) and (items is None or _is_count(items))
    if not readable:
        return DecomposeRun(run_id=run_id if isinstance(run_id, str) else "", wrote_items=True, refusal_line=None)
    verdict = log_wrote_items(run_id, log or "")
    return DecomposeRun(
        run_id=run_id, wrote_items=items != 0 if verdict is None else verdict, refusal_line=refusal_line(log or "")
    )


def select_runs(rows: Sequence[Mapping[str, Any]], intake_id: str) -> list[Mapping[str, Any]]:
    """The `runs` rows whose id is `<intake_id>-<n>`, oldest `launched_at` first."""
    own = (r for r in rows if run_initiative(str(r.get("run_id") or "")) == intake_id)
    return sorted(own, key=lambda r: str(r.get("launched_at") or ""))


# Deliberate process-lifetime cache, a bend of charter A4: one parse per (run_id, log mtime_ns).
_REFUSALS: dict[tuple[str, int], tuple[str | None, bool | None]] = {}


def _log_facts(runs_dir: Path, run_id: str) -> tuple[str | None, bool | None]:
    """Edge. The log's (refusal line, wrote_items verdict), parsed once per (run_id, mtime_ns); (None, None) if missing."""
    path = runs_dir / f"{run_id}.log"
    try:
        key = (run_id, path.stat().st_mtime_ns)
        if key not in _REFUSALS:
            text = path.read_text(encoding="utf-8", errors="replace")
            _REFUSALS[key] = (refusal_line(text), log_wrote_items(run_id, text))
        return _REFUSALS[key]
    except OSError:
        return (None, None)


def _task_items(runs_dir: Path, initiative: str) -> int | None:
    """None when the harness python is missing, since `run_store.work_items` then reads as empty."""
    if run_store._harness_python() is None:
        return None
    return sum(1 for row in run_store.work_items(runs_dir, initiative) if row.get("kind", "task") == "task")


def read_decompose_runs(runs_dir: Path, intake_id: str) -> list[DecomposeRun]:
    """Edge. A run that has not exited gets `task_items` None; the intake id is the `chair_exec.decompose_id` value."""
    items = _task_items(runs_dir, intake_id)
    out = []
    for row in select_runs(exit_rows(runs_dir), intake_id):
        exited = row_exited(row)
        run = parse_run({"run_id": str(row["run_id"]), "log": None, "task_items": items if exited else None})
        refusal, verdict = _log_facts(runs_dir, run.run_id)
        wrote = verdict if exited and verdict is not None else run.wrote_items
        out.append(replace(run, wrote_items=wrote, refusal_line=refusal))
    return out
