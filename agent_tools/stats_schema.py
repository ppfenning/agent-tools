"""SQLite schema and migration helper for the stats store (workspace/stats/stats.db).

Spec: docs/design/run-stats-store.md §3. Spike verdict:
agent_tools/stats-join-spike.md reports "heuristic join, error rate 7.1%" — not
"no join" — so `calls.task_id` and `calls.join_confidence` are KEPT rather than
dropped, and `join_confidence` is typed to hold that marker ("exact" or
"heuristic") per call row. §5's aggregates therefore stay call-grain, joined
through `task_id`, with `join_confidence` available to exclude or flag
low-confidence rows rather than being defined at run grain.

No ingest logic, no CLI wiring, no query functions here: schema and migration
only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

OUTCOMES = ("landed", "quarantined", "budget_stop", "skipped", "unknown")
FAILURE_CLASSES = ("budget_stop", "tool_error", "empty_patch", "refused", "ok", "unknown")


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    default: object = None
    not_null: bool = False


def column_ddl(col: Column) -> str:
    parts = [col.name, col.sql_type]
    if col.not_null:
        parts.append("NOT NULL")
    if col.default is not None:
        default = f"'{col.default}'" if isinstance(col.default, str) else str(col.default)
        parts.append(f"DEFAULT {default}")
    return " ".join(parts)


RUNS_COLUMNS = (
    Column("run_id", "TEXT"),
    Column("started_at", "TEXT"),
    Column("ended_at", "TEXT"),
    Column("cartridge_sha", "TEXT"),
    Column("cartridge_team", "TEXT"),
    Column("provider_profile", "TEXT"),
    Column("provider_profile_sha", "TEXT"),
    Column("vendor", "TEXT", default="claude-code"),
    Column("host", "TEXT"),
    Column("launched_by", "TEXT"),
    Column("principal", "TEXT"),
    Column("human_minutes", "REAL"),
    Column("schema_version", "INTEGER"),
)

CALLS_COLUMNS = (
    Column("run_id", "TEXT"),
    Column("seq", "INTEGER"),
    Column("role", "TEXT"),
    Column("attempt", "INTEGER"),
    Column("tier", "TEXT"),
    Column("model", "TEXT"),
    Column("model_id", "TEXT"),
    Column("cost_usd", "REAL"),
    Column("turns", "INTEGER"),
    Column("duration_ms", "INTEGER"),
    Column("input_tokens", "INTEGER"),
    Column("cache_read_tokens", "INTEGER"),
    Column("cache_creation_tokens", "INTEGER"),
    Column("output_tokens", "INTEGER"),
    Column("tools", "TEXT"),
    Column("trace_path", "TEXT"),
    Column("failure_class", "TEXT"),
    Column("challenger", "INTEGER", default=0, not_null=True),
    Column("task_id", "TEXT"),
    Column("join_confidence", "TEXT"),
    Column("recovered_from_trace", "INTEGER", default=0, not_null=True),
)

# A since-date filter joins tasks to runs on run_id and reads runs.started_at; tasks carry no timestamp of their own.
TASKS_COLUMNS = (
    Column("run_id", "TEXT"),
    Column("task_id", "TEXT"),
    Column("ticket", "TEXT"),
    Column("phase", "TEXT"),
    Column("initiative", "TEXT"),
    Column("attempt", "INTEGER"),
    Column("outcome", "TEXT"),
    Column("outcome_source", "TEXT"),
    Column("review_rounds", "INTEGER"),
    Column("arbitration_verdict", "TEXT"),
    Column("fix_loop_rounds", "INTEGER"),
    Column("cost_usd", "REAL"),
    Column("reason", "TEXT"),
    Column("outcome_kind", "TEXT"),
    # Gate facts: one scalar per task, like arbitration_verdict above, so columns rather than a child table. NULL means the record did not say.
    Column("handoff_verdict", "TEXT"),
    Column("charter_verdict", "TEXT"),
    Column("adversary_verdict", "TEXT"),
    Column("arbiter_verdict", "TEXT"),
    Column("arbiter_sided_with", "TEXT"),
    Column("arbiter_state", "TEXT"),
    Column("fix_loop_attempts", "INTEGER"),
    Column("fix_loop_stopped", "INTEGER"),
    Column("plan_gate_verdict", "TEXT"),
)

TABLES = {
    "runs": (RUNS_COLUMNS, "run_id"),
    "calls": (CALLS_COLUMNS, None),
    "tasks": (TASKS_COLUMNS, None),
}


def table_sql(name: str, columns: tuple[Column, ...], primary_key: str | None) -> str:
    lines = [column_ddl(col) for col in columns]
    if primary_key is not None:
        lines.append(f"PRIMARY KEY ({primary_key})")
    body = ",\n    ".join(lines)
    return f"CREATE TABLE IF NOT EXISTS {name} (\n    {body}\n)"


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def ensure_schema(conn: sqlite3.Connection) -> None:
    for name, (columns, primary_key) in TABLES.items():
        conn.execute(table_sql(name, columns, primary_key))
        present = _existing_columns(conn, name)
        for col in columns:
            if col.name not in present:
                conn.execute(f"ALTER TABLE {name} ADD COLUMN {column_ddl(col)}")
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    ensure_schema(conn)
    return conn
