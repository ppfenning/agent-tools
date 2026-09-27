"""The `history` source of `chair_facts.FactsDeps`: the newest `chair_actions` row of kind `housekeeping`."""

from collections.abc import Iterable, Mapping
from pathlib import Path

from agent_tools import run_store

SKIPPED_STATUSES = frozenset({"dry_run", "refused"})


def latest_housekeeping(rows: Iterable[Mapping]) -> str | None:
    """The greatest `ts` among rows whose `kind` is `housekeeping`; None with no such row.

    A row whose `status` is `dry_run` or `refused` did not actually run housekeeping, and a row
    with no `ts` cannot be compared, so both are left out of the candidates."""
    candidates = [
        str(row["ts"])
        for row in rows
        if row.get("kind") == "housekeeping" and row.get("status") not in SKIPPED_STATUSES and row.get("ts")
    ]
    return max(candidates) if candidates else None


def read_last_housekeeping(runs_dir: Path) -> str | None:
    """Edge. None with no store, no `chair_actions` table, an unreadable store, or the harness (psycopg) absent.

    A history read that cannot be trusted is treated the same as a store with no housekeeping row: the plan
    rule sees `None` and treats housekeeping as due rather than stalling because the store could not be read.
    """
    try:
        opened = run_store._open(runs_dir)
    except (*run_store._DB_ERRORS, RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return None
    if opened is None:
        return None
    conn, token = opened
    try:
        sql = run_store._sql("SELECT ts, status FROM chair_actions WHERE kind = {p}", token)
        rows = [dict(r) for r in conn.execute(sql, ("housekeeping",)).fetchall()]
    except run_store._DB_ERRORS:
        return None
    finally:
        conn.close()
    return latest_housekeeping([{**row, "kind": "housekeeping"} for row in rows])
