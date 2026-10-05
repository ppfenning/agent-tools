"""The chair's per-tick status: the JSON object stored in the `status` column of the chair lease row.

The standby reads the row through `chair_read_lease.read_lease`, which runs `SELECT *` and uses only holder, expires_at and
epoch, so a JSON object in `status` cannot break it. unknown: whether the harness's `leases` table has a `status` column;
nothing in agent_tools names one, and an UPDATE against a table without it reads as a database error and returns False.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.chair import LEASE_NAME
from agent_tools.store_dialect import is_postgres, placeholder

UPDATE_SQL = "UPDATE leases SET status = {p} WHERE name = {p} AND epoch = {p}"


def build_tick_status(line: str, now: datetime, action: dict[str, Any] | None) -> dict[str, Any]:
    """Pure. `now` is injected and read as UTC; `action` is {kind, target, since} or None."""
    tick_at = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"tick_at": tick_at, "status": line, "current_action": None if action is None else dict(action)}


def dump_tick_status(status: dict[str, Any]) -> str:
    """Pure. The JSON text stored in the column."""
    return json.dumps(status, sort_keys=True, separators=(",", ":"))


def _open_writable(runs_dir: Path) -> tuple[Any, str] | None:
    """Edge. A writable connection and its bind token; None when a SQLite store file is absent, so it never creates one.

    `run_store._open` is read-only (`mode=ro`, `read_only = True`), so an UPDATE through it always fails."""
    url = run_store._store_url(runs_dir)
    if is_postgres(url):
        try:
            import psycopg
        except ImportError:
            return None
        return psycopg.connect(url), placeholder(url)
    path = run_store._sqlite_path(url)
    return (sqlite3.connect(path), placeholder(url)) if path.exists() else None


def write_tick_status(runs_dir: Path, epoch: int, text: str) -> bool:
    """Edge. True when the chair lease row at `epoch` took `text`; False with no store, a database error, or a stale epoch."""
    try:
        opened = _open_writable(Path(runs_dir))
    except (*run_store._DB_ERRORS, RuntimeError):
        return False
    if opened is None:
        return False
    conn, token = opened
    try:
        updated = conn.execute(run_store._sql(UPDATE_SQL, token), (text, LEASE_NAME, epoch)).rowcount
        conn.commit()
    except (*run_store._DB_ERRORS, RuntimeError):
        return False
    finally:
        conn.close()
    return updated == 1
