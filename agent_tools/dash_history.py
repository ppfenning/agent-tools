"""Run history for the dash feed, derived from store rows. Pure: rows in, plain data out; the caller injects `now` and `tz`.

Names come from the store and the harness, none are new. `runs` rows carry run_id, ended_at, host and status (`dash_feed._finished_status`
reads approved, landed, quarantined; the store writes `ok` for a run that finished, `error` or `failed` for one that did not).
`task_records` rows carry run_id, phase_id, task_id and record_json. A record names `landed` (True, or the `{at, pr}` that
`store_cli.mark_landed` writes), `ticket`, `initiative`, `cause`, `reason`, the review sections `land._approved` reads, and an
`attempts` list whose entries carry `cause` (`stats_ingest`, `chair_read_attempts`). `node_calls` rows carry run_id and cost_usd.
A stopped run is one whose status is in STOPPED_STATUSES; `budget` is `runs_top._status`. Outcomes: landed, approved, quarantined,
stopped, crashed, and idle for a run that finished with nothing landed, approved or quarantined.
unknown: the store's own status for a stopped run. The PR is read from the record's `pr`, else its `landed.pr`; a number is
taken from the end of its URL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, tzinfo
from itertools import groupby
from typing import Any

from agent_tools.land import _approved as land_approved

Row = Mapping[str, Any]

LIMIT = 30
STOPPED_STATUSES = ("stopped", "budget")
CRASHED_STATUSES = ("error", "failed", "never_recorded")


def _record(raw: Any) -> Mapping[str, Any]:
    """A `record_json` cell as a dict, from SQLite text or a decoded value; empty when it is not a JSON object."""
    if isinstance(raw, str):
        try:
            return _record(json.loads(raw))
        except ValueError:
            return {}
    return raw if isinstance(raw, Mapping) else {}


def _ended(text: Any) -> datetime | None:
    """An `ended_at` as an aware datetime; a naive one reads as UTC, and an empty or unparseable one is None."""
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _by_run(rows: Iterable[Row]) -> dict[str, list[Row]]:
    """Rows grouped by run_id, each group in input order."""
    return {run: list(group) for run, group in groupby(sorted(rows, key=lambda r: str(r.get("run_id"))), key=lambda r: str(r.get("run_id")))}


def _in_task_order(run_tasks: Iterable[Row]) -> list[Row]:
    """A run's task_records rows by phase_id then task_id."""
    return sorted(run_tasks, key=lambda r: (str(r.get("phase_id") or ""), str(r.get("task_id") or "")))


def _is_landed(row: Row) -> bool:
    """`landed` read the way `runs_stranded` reads it: True and mark-landed's `{at, pr}` both count."""
    return bool(_record(row.get("record_json")).get("landed"))


def _is_approved(row: Row) -> bool:
    """The land's own approval rule, `land._approved`, so a unanimous approval with the arbiter skipped counts too."""
    return land_approved(dict(_record(row.get("record_json")))) is None


def first_cause(run_tasks: Iterable[Row]) -> str:
    """The first quarantine cause in task order: an attempt's `cause`, else a record's `cause`, else its `reason`; empty when none."""
    records = [_record(row.get("record_json")) for row in _in_task_order(run_tasks)]
    attempts = [a for record in records for a in (record.get("attempts") or []) if isinstance(a, Mapping)]
    own = [str(r[key]) for key in ("cause", "reason") for r in records if r.get(key)]
    causes = [str(a["cause"]) for a in attempts if a.get("cause")] + own
    return causes[0] if causes else ""


def run_outcome(run: Row, run_tasks: Sequence[Row]) -> str:
    """The one outcome rule. A store status of landed, approved or quarantined wins; then what the records built: landed, approved,
    a quarantine cause; then stopped; crashed for an error status, or a missing/unknown one with no records; else idle."""
    status = run.get("status")
    if status in ("landed", "approved", "quarantined"):
        return str(status)
    if any(_is_landed(row) for row in run_tasks):
        return "landed"
    if any(_is_approved(row) for row in run_tasks):
        return "approved"
    if first_cause(run_tasks):
        return "quarantined"
    if status in STOPPED_STATUSES:
        return "stopped"
    if status in CRASHED_STATUSES or (status != "ok" and not run_tasks):
        return "crashed"
    return "idle"


def _pr_number(raw: Any) -> int | None:
    """The PR number from a record's `pr`: an int as is, a URL by its trailing digits, None when there are none."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    found = re.search(r"(\d+)$", str(raw or "").strip().rstrip("/"))
    return int(found.group(1)) if found else None


def _landed_pr(landed: Any) -> Any:
    return landed.get("pr") if isinstance(landed, Mapping) else None


def landed_tasks(run_tasks: Iterable[Row]) -> list[dict[str, Any]]:
    """`{task, pr}` for each landed task in task order; `pr` is None when the record names no PR number."""
    return [
        {"task": record.get("ticket") or row.get("task_id"), "pr": _pr_number(record.get("pr") or _landed_pr(record.get("landed")))}
        for row in _in_task_order(run_tasks)
        if _is_landed(row)
        for record in (_record(row.get("record_json")),)
    ]


def _cost(calls: Iterable[Row]) -> float:
    return float(sum(call.get("cost_usd") or 0.0 for call in calls))


def _initiative(run: Row, run_tasks: Iterable[Row]) -> str:
    """The run's own initiative, else the first record's, else the run id without its trailing `-N` run number."""
    named = [str(i) for row in _in_task_order(run_tasks) if (i := _record(row.get("record_json")).get("initiative"))]
    return str(run.get("initiative") or (named[0] if named else "") or re.sub(r"-\d+$", "", str(run.get("run_id") or "")))


def _ended_runs(runs: Iterable[Row]) -> list[tuple[datetime, Row]]:
    """(ended_at, run) for every run that has ended, newest first; a run with no end is not ended."""
    ended = [(at, run) for run in runs if (at := _ended(run.get("ended_at"))) is not None]
    return sorted(ended, key=lambda pair: (pair[0], str(pair[1].get("run_id"))), reverse=True)


def build_history(runs: Sequence[Row], task_records: Sequence[Row], node_calls: Sequence[Row], limit: int = LIMIT) -> list[dict[str, Any]]:
    """Up to `limit` rows for ended runs, newest ended first. Only a quarantined row carries `cause`."""
    tasks, calls = _by_run(task_records), _by_run(node_calls)
    rows = []
    for _, run in _ended_runs(runs)[:limit]:
        run_id = str(run.get("run_id"))
        run_tasks = tasks.get(run_id, [])
        outcome = run_outcome(run, run_tasks)
        rows.append({
            "run": run_id,
            "machine": run.get("host") or "",
            "initiative": _initiative(run, run_tasks),
            "ended_at": run.get("ended_at"),
            "outcome": outcome,
            "cost_usd": _cost(calls.get(run_id, [])),
            "landed": landed_tasks(run_tasks),
            **({"cause": first_cause(run_tasks)} if outcome == "quarantined" else {}),
        })
    return rows


def history_today(runs: Sequence[Row], task_records: Sequence[Row], node_calls: Sequence[Row], now: datetime, tz: tzinfo) -> dict[str, Any]:
    """Counts over every run ended since local midnight in `tz`, not only the newest `LIMIT`."""
    local = now.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    today = [run for at, run in _ended_runs(runs) if at >= midnight]
    tasks, calls = _by_run(task_records), _by_run(node_calls)
    outcomes = [run_outcome(run, tasks.get(str(run.get("run_id")), [])) for run in today]
    return {
        "lands": outcomes.count("landed"),
        "quarantines": outcomes.count("quarantined"),
        "cost_usd": float(sum(_cost(calls.get(str(run.get("run_id")), [])) for run in today)),
        "runs": len(today),
    }
