"""The data behind `cox dash --detail machine <name>`: the v1 machine shape --
schema, kind, the current host facts off the host's most recent beat, its
checkouts keyed by repo with branch and commits-behind-main, and a count of
lanes still running. `build` is the thin edge: `run_store.hosts` for the
host's row, a read of its own run records, then the pure core
`build_machine_detail`.

`host_facts` and `checkouts` come from `host_cmd.beat_versions`'s own
`host_facts`/`checkouts` keys in the host row's `versions_json` -- the
producer a `cox host beat` writes. A host that never beat with the current
producer, or whose beat is missing a fact, reads `""`/`0` for that fact
rather than raising."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import run_store

try:
    import psycopg

    _DB_ERRORS: tuple[type[BaseException], ...] = (sqlite3.DatabaseError, psycopg.Error)
except ImportError:
    _DB_ERRORS = (sqlite3.DatabaseError,)

__all__ = ["build", "build_machine_detail"]


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


def _host_facts(versions: Mapping[str, Any]) -> dict:
    """`{os, cpu_count, mem_total_gb}` off `beat_versions`'s own `host_facts` key; `""`/`0` for a host
    with no beat, or a beat missing a fact."""
    facts = versions.get("host_facts")
    facts = facts if isinstance(facts, Mapping) else {}
    return {
        "os": facts.get("os") or "",
        "cpu_count": facts.get("cpu_count") or 0,
        "mem_total_gb": facts.get("mem_total_gb") or 0,
    }


def _checkouts(versions: Mapping[str, Any]) -> dict:
    """`beat_versions`'s own `checkouts` dict, reshaped to exactly `{behind_main, branch}` per repo so an
    extra key a future producer adds never reaches the v1 output."""
    checkouts = versions.get("checkouts")
    checkouts = checkouts if isinstance(checkouts, Mapping) else {}
    return {
        str(repo): {"behind_main": entry.get("behind_main") if isinstance(entry, Mapping) else None,
                    "branch": entry.get("branch") if isinstance(entry, Mapping) else None}
        for repo, entry in checkouts.items()
    }


def _lanes_in_use(runs: Sequence[Mapping[str, Any]]) -> int:
    """Count of this host's own run records still running (`ended_at` is None); the full lane history
    never reaches the v1 output."""
    return sum(1 for run in runs if run.get("ended_at") is None)


def build_machine_detail(host_row: Mapping[str, Any] | None, runs: Sequence[Mapping[str, Any]], now: str) -> dict:
    """Pure core: a `hosts`-table row (or None) plus this host's own run records into the v1
    `dash --detail machine` dict. `now` is the caller's own clock reading, ISO-8601 with a Z suffix,
    never `datetime.now()` here, so this stays a pure function of its arguments."""
    versions = _versions(host_row)
    return {
        "schema": 1,
        "kind": "machine",
        "at": now,
        "machine": host_row.get("name") if host_row is not None else None,
        "host_facts": _host_facts(versions),
        "checkouts": _checkouts(versions),
        "lanes_in_use": _lanes_in_use(runs),
        "capacity": host_row.get("capacity") if host_row is not None else None,
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
    that host, oldest first, fed to the pure `build_machine_detail`."""
    host_row = next((row for row in run_store.hosts(runs_dir) if row.get("name") == host_name), None)
    conn = run_store.connect_readonly(runs_dir)
    if conn is None:
        return build_machine_detail(host_row, [], now)
    try:
        runs = _host_runs(conn, host_name)
    finally:
        conn.close()
    with_heartbeat = [{**run, "heartbeat_at": _heartbeat_at(runs_dir, run["run_id"])} for run in runs]
    return build_machine_detail(host_row, with_heartbeat, now)
