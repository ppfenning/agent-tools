from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.chair_facts import run_initiative

Row = Mapping[str, Any]


def row_exited(row: Row) -> bool:
    """Pure. A `runs` row has exited when it carries an `ended_at` or its status is `quarantined`."""
    return row.get("ended_at") is not None or row.get("status") == "quarantined"


def run_exited(rows: Sequence[Row]) -> dict[str, bool]:
    """Pure. Per initiative, whether its newest run by `launched_at` has an `ended_at` or status `quarantined`; an initiative with no row is absent."""
    newest: dict[str, Row] = {}
    for row in rows:
        initiative = run_initiative(str(row.get("run_id") or ""))
        held = newest.get(initiative)
        if held is None or str(row.get("launched_at") or "") > str(held.get("launched_at") or ""):
            newest[initiative] = row
    return {i: row_exited(row) for i, row in newest.items()}


def exit_rows(runs_dir: Path) -> list[dict[str, Any]]:
    """Edge. run_id, launched_at, ended_at and status of every `runs` row; empty with no store or an unreadable one. A store with no `status` column reads it as None."""
    opened = run_store._open(runs_dir)
    if opened is None:
        return []
    conn, token = opened
    try:
        status = "status" if "status" in run_store._runs_columns(conn, token) else "NULL AS status"
        return [dict(r) for r in conn.execute(f"SELECT run_id, launched_at, ended_at, {status} FROM runs").fetchall()]
    except run_store._DB_ERRORS:
        return []
    finally:
        conn.close()


def exits(runs_dir: Path) -> dict[str, bool]:
    """Edge. `run_exited` over the store's `runs` rows."""
    return run_exited(exit_rows(runs_dir))
