"""The store rows the dash feed history is built from: ended runs, and the task records and call rows of those runs."""

from pathlib import Path
from typing import Any

from agent_tools.run_store import _DB_ERRORS, _open, _sql

RECENT_ENDED = 30

_RUNS = (
    "SELECT * FROM runs WHERE ended_at IS NOT NULL AND (ended_at >= {p} OR run_id IN "
    "(SELECT run_id FROM runs WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT " + str(RECENT_ENDED) + ")) "
    "ORDER BY ended_at DESC"
)


def _empty() -> dict[str, list[dict[str, Any]]]:
    return {"runs": [], "task_records": [], "node_calls": []}


def read_history_rows(store: Path, since: str) -> dict[str, list[dict[str, Any]]]:
    """Edge. {runs, task_records, node_calls} as the store holds them, column names unchanged. `runs` are ended runs with `ended_at >= since` (ISO text) or among the newest 30 ended; the other two cover those run ids only. Empty with no store or an unreadable one."""
    try:
        opened = _open(store)
    except (*_DB_ERRORS, RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return _empty()
    if opened is None:
        return _empty()
    conn, token = opened
    try:
        runs = [dict(r) for r in conn.execute(_sql(_RUNS, token), (since,)).fetchall()]
        ids = tuple(run["run_id"] for run in runs)
        if not ids:
            return _empty()
        marks = ", ".join("{p}" for _ in ids)
        by_ids = {
            table: [dict(r) for r in conn.execute(_sql(f"SELECT * FROM {table} WHERE run_id IN ({marks})", token), ids).fetchall()]
            for table in ("task_records", "node_calls")
        }
    except _DB_ERRORS:
        return _empty()
    finally:
        conn.close()
    return {"runs": runs, **by_ids}
