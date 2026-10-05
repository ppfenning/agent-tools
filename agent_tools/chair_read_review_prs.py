"""The `review_prs` source of a chair tick: approved tasks whose record names a review PR.

`awaiting_reviews` is pure. `read_review_prs` is the edge: it reads the task records under the runs directory and
asks the injected `pr_state` for each awaiting url. It never calls the forge or gh itself.
"""
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_tools import run_store
from agent_tools.chair_facts import run_initiative
from agent_tools.chair_types import ReviewPr

Record = Mapping[str, Any]
PrState = Mapping[str, Any]
_NO_HOSTS: Mapping[str, str] = MappingProxyType({})


def _url(record: Record) -> str:
    url = record.get("review_pr")
    return url if isinstance(url, str) else ""


def _awaiting(record: Record) -> bool:
    return record.get("status") == "approved" and bool(_url(record))


def _owner(record: Record, hosts: Mapping[str, str]) -> dict[str, str]:
    """`run` of the record that owns the review url, and `host` when the store has a row for that run ("" is the chair machine)."""
    run = str(record.get("run") or "")
    return {**({"run": run} if run else {}), **({"host": hosts[run]} if run in hosts else {})}


def awaiting_reviews(records: Sequence[Record], states: Mapping[str, PrState], hosts: Mapping[str, str] = _NO_HOSTS) -> list[ReviewPr]:
    """One row per approved record with a non-empty `review_pr`. A url missing from `states` has state "unknown".

    A row names the owning `run`, and its `host` only when `hosts` has the run; a run with no row has no `host` key.
    """
    return [
        {
            "initiative": str(record.get("initiative") or run_initiative(str(record.get("run", "")))),
            "phase": str(record.get("phase", "")),
            "task_id": str(record.get("task_id") or record.get("task", "")),
            "repo": str(record.get("repo") or ""),
            "url": _url(record),
            "state": states.get(_url(record), {}).get("state", "unknown"),
            "merged_at": states.get(_url(record), {}).get("merged_at"),
            **_owner(record, hosts),
        }  # type: ignore[typeddict-item]  # run and host ride beside ReviewPr's fields
        for record in records
        if _awaiting(record)
    ]


def store_run_hosts(runs_dir: Path, runs: Sequence[str]) -> dict[str, str]:
    """Edge. Each run's `runs.host` ("" when blank or the column is absent); a run with no row is omitted.

    Empty with no store or an unreadable one, so every row then lacks a host and the executor sends it to the chair.
    """
    opened = run_store._open(runs_dir)
    if opened is None:
        return {}
    conn, token = opened
    try:
        host_expr = "host" if "host" in run_store._runs_columns(conn, token) else "NULL AS host"
        sql = run_store._sql(f"SELECT {host_expr} FROM runs WHERE run_id = {{p}}", token)
        rows = {run: conn.execute(sql, (run,)).fetchone() for run in runs}
    except run_store._DB_ERRORS:
        return {}
    finally:
        conn.close()
    return {run: (row["host"] or "") for run, row in rows.items() if row is not None}


def _load_records(runs_dir: Path) -> list[dict]:
    records = []
    for path in sorted(runs_dir.glob("*/tasks/*/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records.append({"run": path.parent.parent.parent.name, "task": path.stem, "phase": path.parent.name, **record})
    return records


def read_review_prs(
    runs_dir: Path,
    pr_state: Callable[[str], PrState],
    run_hosts: Callable[[Path, Sequence[str]], Mapping[str, str]] = store_run_hosts,
) -> list[ReviewPr]:
    """Edge. Calls `pr_state(url)` once per awaiting record and `run_hosts` once, and hands both to `awaiting_reviews`."""
    records = _load_records(Path(runs_dir))
    awaiting = [r for r in records if _awaiting(r)]
    hosts = run_hosts(Path(runs_dir), sorted({str(r.get("run", "")) for r in awaiting})) if awaiting else {}
    return awaiting_reviews(records, {_url(r): pr_state(_url(r)) for r in awaiting}, hosts)
