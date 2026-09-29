"""The data behind `cox dash --detail machine <name>`: a host's facts (state,
capacity, beat age, login), each checkout its own beat recorded, and its lane
history. `build` is the thin edge: `run_store.hosts` for the host's row, a
read of its own run records, then the pure core `_shape`.

No reader in this store counts commits behind main per checkout yet (a search
of this repo for `behind_main` and `commits_behind` found none outside the
unrelated route-sync divergence check in `cli.py`'s `sync_decision`), so each
checkout carries `commits_behind_main: None` rather than a new git-comparison
routine or a live probe of the host."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_tools import host_cmd, run_store

try:
    import psycopg

    _DB_ERRORS: tuple[type[BaseException], ...] = (sqlite3.DatabaseError, psycopg.Error)
except ImportError:
    _DB_ERRORS = (sqlite3.DatabaseError,)

__all__ = ["build"]


def _versions(row: Mapping[str, Any] | None) -> dict:
    """`versions_json` as a dict: a JSON string from SQLite, already a mapping from a Postgres JSONB column."""
    raw = row.get("versions_json") if row is not None else None
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _beat_age_s(beat_at: Any, now: str) -> int | None:
    """Seconds between `beat_at` and `now`; None when either is missing or unparsable."""
    if not beat_at:
        return None
    try:
        beat = datetime.fromisoformat(str(beat_at).replace("Z", "+00:00"))
        at = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(int((at - beat).total_seconds()), 0)


def _checkouts(host_row: Mapping[str, Any] | None) -> list[dict]:
    """One row per checkout path the host's own beat recorded, `commits_behind_main` always None."""
    return [{"repo": repo, "commits_behind_main": None} for repo in host_cmd.recorded_repos(host_row)]


def _shape(host_row: Mapping[str, Any] | None, runs: Sequence[Mapping[str, Any]], now: str) -> dict:
    """Pure core: a `hosts`-table row (or None) plus this host's own run records, oldest first, into the `dash --detail machine` dict."""
    versions = _versions(host_row)
    return {
        "host": host_row.get("name") if host_row is not None else None,
        "state": host_row.get("state") if host_row is not None else None,
        "capacity": host_row.get("capacity") if host_row is not None else None,
        "beat_age_s": _beat_age_s(host_row.get("beat_at") if host_row is not None else None, now),
        "login_ok": versions.get("login_ok"),
        "login_checked_at": versions.get("login_checked_at"),
        "checkouts": _checkouts(host_row),
        "lanes": [
            {
                "run": run.get("run_id"),
                "launched_at": run.get("launched_at"),
                "heartbeat_at": run.get("heartbeat_at"),
                "ended_at": run.get("ended_at"),
            }
            for run in runs
        ],
    }


def _run_columns(conn: Any) -> set[str]:
    """Edge: the column names of `runs`, sqlite via `PRAGMA`, Postgres via `information_schema`."""
    if isinstance(conn, sqlite3.Connection):
        return {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    rows = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'runs'").fetchall()
    return {row["column_name"] for row in rows}


def _host_runs(conn: Any, host_name: str) -> list[dict]:
    """Edge: `run_id`, `launched_at`, `ended_at` of every run row of `host_name`, oldest first; empty
    when the store has no `host` column, has no `runs` table, or is unreadable."""
    try:
        if "host" not in _run_columns(conn):
            return []
        rows = conn.execute("SELECT run_id, launched_at, ended_at, host FROM runs ORDER BY launched_at").fetchall()
    except _DB_ERRORS:
        return []
    return [dict(row) for row in rows if row["host"] == host_name]


def _heartbeat_at(runs_dir: Path, run_id: str) -> str | None:
    """The run's current lease heartbeat, reusing `run_store.lease`; None once no lease names this
    run's prefix, as for a run whose lease has long since expired."""
    found = run_store.lease(runs_dir, run_id)
    return found[2] if found is not None else None


def build(host_name: str, runs_dir: Path, now: str) -> dict:
    """Edge: `run_store.hosts` for `host_name`'s own row, plus every run row its own beat marked with
    that host, oldest first, fed to the pure `_shape`."""
    host_row = next((row for row in run_store.hosts(runs_dir) if row.get("name") == host_name), None)
    conn = run_store.connect_readonly(runs_dir)
    if conn is None:
        return _shape(host_row, [], now)
    try:
        runs = _host_runs(conn, host_name)
    finally:
        conn.close()
    with_heartbeat = [{**run, "heartbeat_at": _heartbeat_at(runs_dir, run["run_id"])} for run in runs]
    return _shape(host_row, with_heartbeat, now)
