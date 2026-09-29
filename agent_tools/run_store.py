"""A run's usage: the `<run_id>.usage.json` file first, the read-only SQLite
store `cox.db` only once that file is gone, and only for an ended run. A run has
ended when its `runs` row has an `ended_at`, or when it has no live pid: a
killed run never stamps `ended_at`. Calls alone do not mean the run is done:
graphs writes each call as it finishes. `usages` lists every run that way, and `run_started`
reads a run's `launched_at`. `phase_manifests` reads a run's
`<run_id>:<phase>.json` files first, then the `manifest_record` on the store's
phase rows, ended run or not: a recorded phase is final. Reads only; never
creates, migrates or writes the store."""

from __future__ import annotations

import functools
import io
import json
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_tools import epic
from agent_tools.lake_config import _LITERAL_SECRETS, _from_env
from agent_tools.store_dialect import connect_readonly_url, is_postgres, placeholder
from agent_tools.store_url import TracesRoot, profile_traces_root, read_provider_profile, resolve_store_url

try:
    import psycopg

    _DB_ERRORS: tuple[type[BaseException], ...] = (sqlite3.DatabaseError, psycopg.Error)
except ImportError:
    _DB_ERRORS = (sqlite3.DatabaseError,)

__all__ = [
    "Lane", "ParquetCheck", "TracesUnavailable", "all_phase_manifests", "attempt_causes", "attempt_causes_for", "build_counts",
    "call_events", "call_from_row", "connect_readonly", "cost_since", "efficiency_rows", "gate_call_rows",
    "harness_python", "hosts", "last_call_at", "lease", "live_lanes", "parquet_readable", "phase_manifests", "phase_names", "remote_lanes", "run_ids", "run_spans",
    "run_started", "store_usages", "summarize", "task_verdict_rows", "usage", "usages",
]

STORE_FILENAME = "cox.db"
_PHASE_FILE = re.compile(r"^[^:]+:(.+)\.json$")
TRACES_DIRNAME = "traces"


class TracesUnavailable(Exception):
    """The trace store is needed but an optional package (`zstandard`, or `pyarrow` for Parquet traces) is not installed."""


# Columns that carry over unchanged from a node_calls row to a usage-file call.
_SAME = (
    "role", "task_id", "tier", "cost_usd", "ceiling_usd", "ceiling_source", "turns", "duration_ms",
    "input_tokens", "cache_read_tokens", "cache_creation_tokens", "input_total", "output_tokens", "ts",
)


def summarize(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pure: totals and a per-model breakdown over the recorded calls."""
    fields = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")

    def total(call: Mapping[str, Any]) -> int:
        # Older records carried only a summed `input_tokens`; newer ones split it.
        return int(call.get("input_total") if call.get("input_total") is not None else call.get("input_tokens") or 0)

    by_model: dict[str, dict[str, Any]] = {}
    for call in calls:
        row = by_model.setdefault(str(call.get("model")), {"calls": 0, "cost_usd": 0.0, "input_total": 0, **dict.fromkeys(fields, 0)})
        row["calls"] += 1
        row["cost_usd"] = round(row["cost_usd"] + float(call.get("cost_usd") or 0.0), 4)
        row["input_total"] += total(call)
        for f in fields:
            row[f] += int(call.get(f) or 0)
    return {
        "calls": len(calls),
        "cost_usd": round(sum(float(c.get("cost_usd") or 0.0) for c in calls), 4),
        "turns": sum(int(c.get("turns") or 0) for c in calls),
        "input_total": sum(total(c) for c in calls),
        **{f: sum(int(c.get(f) or 0) for c in calls) for f in fields},
        "by_model": by_model,
    }


def _json_cell(raw: Any) -> Any:
    """A store JSON cell decoded: SQLite hands back text, Postgres a decoded value. None when the text is not JSON."""
    if isinstance(raw, (str, bytes)):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def _detail_of(row: Mapping[str, Any]) -> dict[str, Any]:
    """The row's `detail_json` as an object; empty when the column is absent, null, unparseable or not an object."""
    raw = row["detail_json"] if "detail_json" in row.keys() else None  # noqa: SIM118 -- sqlite3.Row's `in` tests values, not keys
    detail = _json_cell(raw)
    return detail if isinstance(detail, dict) else {}


def call_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `node_calls` row as a usage-file call. `summary` and `commands_run` ride along from `detail_json` when it holds them."""
    detail = _detail_of(row)
    return {
        **{k: row[k] for k in _SAME},
        "id": row["call_id"],
        "model": row["model_alias"],
        "ok": bool(row["ok"]),
        "decision": _json_cell(row["decision_json"]),
        **{k: detail[k] for k in ("summary", "commands_run") if k in detail},
    }


# mirrors cli.DEFAULT_PROFILE; cli imports this module, so it cannot be imported back
_DEFAULT_PROFILE = "~/.config/agent-tools/profile.yaml"


def _sql(template: str, token: str) -> str:
    """Pure: `template` with each `{p}` marker replaced by the dialect's bind-parameter token."""
    return template.replace("{p}", token)


def _sqlite_path(url: str) -> Path:
    return Path(url.removeprefix("sqlite:///") if url.startswith("sqlite:") else url)


def _store_url(runs_dir: Path) -> str:
    """Edge. The store URL: the provider profile named by the routing profile (`$AGENT_TOOLS_PROFILE` or the default), else `cox.db` in `runs_dir`."""
    return _store_url_for(str(runs_dir), os.environ.get("AGENT_TOOLS_PROFILE") or _DEFAULT_PROFILE)


@functools.lru_cache(maxsize=32)
def _store_url_for(runs_dir: str, routing_profile: str) -> str:
    """Cached per process: liveness asks once per pidfile, and two YAML reads each time made `route context` slow."""
    routing = read_provider_profile(routing_profile)
    named = routing.get("provider_profile")
    return resolve_store_url(named if isinstance(named, str) else "", runs_dir)


def _traces_root(runs_dir: Path) -> TracesRoot:
    """Edge. The traces root: the `traces_url` of the provider profile the routing profile names, else `<runs_dir>/traces`."""
    return _traces_root_for(str(runs_dir), os.environ.get("AGENT_TOOLS_PROFILE") or _DEFAULT_PROFILE)


@functools.lru_cache(maxsize=32)
def _traces_root_for(runs_dir: str, routing_profile: str) -> TracesRoot:
    """Cached per process, like `_store_url_for`: a call's events are asked for once per call."""
    named = read_provider_profile(routing_profile).get("provider_profile")
    return profile_traces_root(read_provider_profile(named) if isinstance(named, str) and named else {}, runs_dir)


def harness_python(routing: Mapping[str, Any]) -> Path | None:
    """Edge: the routing profile's `<harness_dir>/.venv/bin/python` when that file exists, else None."""
    harness_dir = routing.get("harness_dir")
    if not isinstance(harness_dir, str) or not harness_dir:
        return None
    python = Path(harness_dir).expanduser() / ".venv" / "bin" / "python"
    return python if python.is_file() else None


@functools.lru_cache(maxsize=32)
def _harness_python_for(routing_profile: str) -> Path | None:
    """Cached per process: `harness_python` of the routing profile at that path."""
    return harness_python(read_provider_profile(routing_profile))


def _harness_python() -> Path | None:
    """Edge. `_harness_python_for` for `$AGENT_TOOLS_PROFILE` or the default routing profile."""
    return _harness_python_for(os.environ.get("AGENT_TOOLS_PROFILE") or _DEFAULT_PROFILE)


def _open(runs_dir: Path) -> tuple[Any, str] | None:
    """Edge. A read-only connection and its bind-parameter token; None when a SQLite store file is absent."""
    url = _store_url(Path(runs_dir))
    if not is_postgres(url) and not _sqlite_path(url).exists():
        return None
    return connect_readonly_url(url), placeholder(url)


def connect_readonly(runs_dir: Path) -> Any | None:
    """None when a SQLite store is absent. Opened read-only, so it can never create the file. Rows are addressable by column name."""
    opened = _open(runs_dir)
    return None if opened is None else opened[0]


def _lease_name(run_id: str) -> str:
    # mirrors graphs `harness/run_lease.lease_name`: the lease is per prefix, so `x-3` and `x-4` share `runs:x`
    return "runs:" + re.sub(r"-\d+$", "", run_id)


def lease(runs_dir: Path, run_id: str) -> tuple[str, str, str] | None:
    """Edge. The (holder, expires_at, heartbeat_at) of the store lease for `run_id`'s prefix; None with no store, no row, or an unreadable store."""
    return _lease_table(str(runs_dir), int(time.monotonic() // _LEASE_SNAPSHOT_S)).get(_lease_name(run_id))


_LEASE_SNAPSHOT_S = 2  # one read of the leases table serves every liveness check within this window


@functools.lru_cache(maxsize=8)
def _lease_table(runs_dir: str, _window: int) -> dict[str, tuple[str, str, str]]:
    """Every lease row by name, read once per `_LEASE_SNAPSHOT_S` window: a docket asks about ~800 pidfiles."""
    opened = _open(Path(runs_dir))
    if opened is None:
        return {}
    conn, _ = opened
    try:
        rows = conn.execute("SELECT name, holder, expires_at, heartbeat_at FROM leases").fetchall()
    except _DB_ERRORS:
        return {}
    finally:
        conn.close()
    return {r["name"]: (r["holder"], r["expires_at"], r["heartbeat_at"]) for r in rows}


@dataclass(frozen=True)
class Lane:
    """A live lease joined to its newest run row. `host` is None when the store's `runs` table has no `host` column."""

    run: str
    host: str | None
    launched_at: str
    heartbeat_at: str


def _runs_columns(conn: Any, token: str) -> set[str]:
    """Edge. The column names of `runs`, asked of the backend: a failed SELECT would abort a Postgres transaction."""
    if token == placeholder("postgres://"):
        sql = "SELECT column_name AS name FROM information_schema.columns WHERE table_name = {p} AND table_schema = current_schema()"
        return {r["name"] for r in conn.execute(_sql(sql, token), ("runs",)).fetchall()}
    return {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}


def _newest_run(conn: Any, token: str, name: str, host_expr: str) -> Any | None:
    """Edge. The run row of lease `name`'s prefix with the latest `launched_at`; None when the prefix has no run."""
    prefix = name.removeprefix("runs:")
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "-%"
    sql = _sql(
        f"SELECT run_id, launched_at, {host_expr} FROM runs WHERE (run_id = {{p}} OR run_id LIKE {{p}} ESCAPE '\\') ORDER BY launched_at DESC",
        token,
    )
    return next((r for r in conn.execute(sql, (prefix, like)).fetchall() if _lease_name(r["run_id"]) == name), None)


def live_lanes(runs_dir: Path, now: str) -> list[Lane]:
    """Edge. Every `runs:` lease with `expires_at` later than `now` (ISO UTC, passed in), joined to its prefix's newest run row, by lease name.

    A lease with no run row is skipped. Empty with no store or an unreadable one."""
    table = _lease_table(str(runs_dir), int(time.monotonic() // _LEASE_SNAPSHOT_S))
    live = sorted((name, beat) for name, (_, expires, beat) in table.items() if name.startswith("runs:") and expires > now)
    opened = _open(runs_dir) if live else None
    if opened is None:
        return []
    conn, token = opened
    try:
        host_expr = "host" if "host" in _runs_columns(conn, token) else "NULL AS host"
        joined = [(_newest_run(conn, token, name, host_expr), beat) for name, beat in live]
    except _DB_ERRORS:
        return []
    finally:
        conn.close()
    return [Lane(r["run_id"], r["host"], r["launched_at"], beat) for r, beat in joined if r is not None]


def newest_run_hosts(runs_dir: Path, initiatives: Collection[str]) -> dict[str, str]:
    """Edge. Each initiative's newest run's `runs.host` ("" when the column is absent or the row's host is blank).

    An initiative with no run row is omitted. Empty with no store or an unreadable one."""
    opened = _open(runs_dir)
    if opened is None:
        return {}
    conn, token = opened
    try:
        host_expr = "host" if "host" in _runs_columns(conn, token) else "NULL AS host"
        rows = {i: _newest_run(conn, token, f"runs:{i}", host_expr) for i in initiatives}
    except _DB_ERRORS:
        return {}
    finally:
        conn.close()
    return {i: (r["host"] or "") for i, r in rows.items() if r is not None}


def hosts(runs_dir: Path) -> list[dict]:
    """Edge. The `hosts` table's rows by name; empty with no store, no table, or an unreadable store. Read every chair tick, so it never raises."""
    try:
        opened = _open(runs_dir)
    except (*_DB_ERRORS, RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return []
    if opened is None:
        return []
    conn, _ = opened
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM hosts ORDER BY name").fetchall()]
    except _DB_ERRORS:
        return []
    finally:
        conn.close()


def remote_lanes(lanes: Sequence[Lane], local_runs: Collection[str]) -> list[Lane]:
    """Pure: the lanes whose run no local pidfile names, in input order. A run with a pidfile is local even when its lease is live."""
    return [lane for lane in lanes if lane.run not in local_runs]


def run_ids(runs_dir: Path) -> set[str]:
    """Edge. Every `run_id` in the store's `runs` table; empty with no store or an unreadable one."""
    opened = _open(runs_dir)
    if opened is None:
        return set()
    conn, _ = opened
    try:
        return {row["run_id"] for row in conn.execute("SELECT run_id FROM runs")}
    except _DB_ERRORS:
        return set()
    finally:
        conn.close()


_GATE_FACTS = ("handoff_verdict", "charter_verdict", "adversary_verdict", "arbiter_verdict", "fix_loop_attempts",
               "fix_loop_stopped", "plan_gate_verdict")


def _task_verdict_row(record: Mapping[str, Any], run_id: str, task_id: str) -> dict[str, Any]:
    """Pure: one task record as a `stats_gates` task row, through the `gate_facts` the stats.db ingest uses."""
    # stats_ingest imports run_store at module level, so this import waits until call time to avoid the cycle.
    from agent_tools.stats_ingest import gate_facts

    facts = gate_facts(record)
    plan_gate = record.get("plan_gate")
    ran = isinstance(plan_gate, Mapping) and plan_gate.get("ran") is True
    return {
        "run_id": run_id,
        "task_id": task_id,
        **{key: facts[key] for key in _GATE_FACTS},
        "plan_gate_verdict": facts["plan_gate_verdict"] or ("pass" if ran else None),
        "outcome": "landed" if record.get("landed") is True else None,
    }


def _one_per_key(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pure: one row per (run_id, task_id); a key held by records in two phases states no verdict, never one of the two."""
    counts = Counter((row["run_id"], row["task_id"]) for row in rows)
    silent = dict.fromkeys((*_GATE_FACTS, "outcome"))
    return [
        dict(row) if counts[key] == 1 else {"run_id": key[0], "task_id": key[1], **silent}
        for key, row in {(r["run_id"], r["task_id"]): r for r in rows}.items()
    ]


def _record_of(raw: Any) -> Mapping[str, Any] | None:
    """Pure: a `record_json` cell as a dict, from SQLite text or a decoded Postgres value; None when it is not a JSON object."""
    if isinstance(raw, str):
        try:
            return _record_of(json.loads(raw))
        except ValueError:
            return None
    return raw if isinstance(raw, Mapping) else None


# node_calls.task_id is the bare task id, shared by every run that retries it, while task_records key on
# (run_id, phase_id, task_id). Joining on task_id alone would lend a retried task's verdicts to every run's calls,
# so both row kinds carry run_id and `stats_gates.gate_rows` joins on (run_id, task_id). The gate rows carry no
# phase, so records of one task id in two phases of one run collapse to a row with no verdict (`_one_per_key`).
# `since` is one rule for both: a call counts when its ts is at or after it, and a task record counts when its run
# has such a call. task_records has no timestamp, and a run launched before `since` can still call after it.
_SINCE_CALLS = " WHERE ts >= {p}"
_SINCE_TASKS = " WHERE run_id IN (SELECT run_id FROM node_calls WHERE ts >= {p})"


def task_verdict_rows(runs_dir: Path, since: str | None) -> list[dict[str, Any]]:
    """Edge. One `stats_gates` task row per (run_id, task_id) in `task_records`; empty with no store or an unreadable one."""
    opened = _open(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    where, params = ("", ()) if since is None else (_SINCE_TASKS, (since,))
    try:
        cursor = conn.execute(_sql("SELECT run_id, task_id, record_json FROM task_records" + where, p), params)
        rows = [(r["run_id"], r["task_id"], _record_of(r["record_json"])) for r in cursor.fetchall()]
    except _DB_ERRORS:
        return []
    finally:
        conn.close()
    return _one_per_key([_task_verdict_row(record, run_id, task_id) for run_id, task_id, record in rows if record is not None])


def gate_call_rows(runs_dir: Path, since: str | None) -> list[dict[str, Any]]:
    """Edge. `stats_gates` call rows (role, run_id, task_id, cost_usd), `since` as above; empty with no store or an unreadable one."""
    opened = _open(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    where, params = ("", ()) if since is None else (_SINCE_CALLS, (since,))
    try:
        rows = conn.execute(_sql("SELECT role, run_id, task_id, cost_usd FROM node_calls" + where, p), params).fetchall()
        return [{c: r[c] for c in ("role", "run_id", "task_id", "cost_usd")} for r in rows]
    except _DB_ERRORS:
        return []
    finally:
        conn.close()


def cost_since(runs_dir: Path, since: str, until: str | None = None) -> float | None:
    """Edge. Sum of `node_calls.cost_usd` with `since <= ts < until` (ISO text); None with no store or an unreadable one."""
    opened = _open(runs_dir)
    if opened is None:
        return None
    conn, p = opened
    bound, params = (" AND ts < {p}", (since, until)) if until is not None else ("", (since,))
    try:
        row = conn.execute(_sql("SELECT SUM(cost_usd) AS total FROM node_calls WHERE ts >= {p}" + bound, p), params).fetchone()
        return float(row["total"] or 0.0)
    except _DB_ERRORS:
        return None
    finally:
        conn.close()


def last_call_at(runs_dir: Path, run_ids: Sequence[str]) -> dict[str, str]:
    """Edge. Each id in `run_ids` mapped to its newest `node_calls.ts`; a run with no call row is absent.
    `{}` with no store, an unreadable one, or an empty `run_ids`."""
    opened = _open(runs_dir) if run_ids else None
    if opened is None:
        return {}
    conn, p = opened
    try:
        marks = ", ".join(["{p}"] * len(run_ids))
        sql = _sql(f"SELECT run_id, MAX(ts) AS last FROM node_calls WHERE run_id IN ({marks}) GROUP BY run_id", p)
        return {r["run_id"]: r["last"] for r in conn.execute(sql, tuple(run_ids)).fetchall()}
    except _DB_ERRORS:
        return {}
    finally:
        conn.close()


def build_counts(runs_dir: Path, task_ids: Collection[str], runs: Collection[str] = ()) -> dict[str, int]:
    """Edge. Build calls per `task_id` equal to one of `task_ids` or a `<run>:...` composite of one of `runs`; empty with no store or an unreadable one."""
    ids, prefixes = sorted(set(task_ids)), sorted(set(runs))
    opened = _open(runs_dir) if ids or prefixes else None
    if opened is None:
        return {}
    conn, p = opened
    match = " OR ".join(["task_id = {p}"] * len(ids) + ["task_id LIKE {p} ESCAPE '\\'"] * len(prefixes))
    likes = tuple(r.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%" for r in prefixes)
    try:
        sql = _sql(f"SELECT task_id, COUNT(*) AS n FROM node_calls WHERE role = 'build' AND ({match}) GROUP BY task_id", p)
        return {r["task_id"]: int(r["n"]) for r in conn.execute(sql, (*ids, *likes)).fetchall()}
    except _DB_ERRORS:
        return {}
    finally:
        conn.close()


# node_calls.task_id is the bare work-item id, shared by every run that retries it, so a task is its task_id alone
_EFFICIENCY_CALLS = (
    "SELECT substr(ts, 1, 10) AS day, task_id, COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(SUM(turns), 0) AS turns, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, COALESCE(SUM(input_total), 0) AS input_total "
    "FROM node_calls WHERE ts >= {p} GROUP BY substr(ts, 1, 10), task_id"
)
_EFFICIENCY_TASKS = (
    "SELECT task_id, SUM(CASE WHEN role = 'build' THEN 1 ELSE 0 END) AS builds, MAX(ts) AS last_ts "
    "FROM node_calls WHERE task_id IS NOT NULL GROUP BY task_id"
)


def efficiency_rows(runs_dir: Path, since: str) -> dict[str, list[dict[str, Any]]]:
    """Edge. `calls` per (UTC day, task_id) from `since` on; `tasks` per task_id over all runs and time; empty with no store."""
    opened = _open(runs_dir)
    if opened is None:
        return {"calls": [], "tasks": []}
    conn, token = opened
    try:
        calls = [dict(r) for r in conn.execute(_sql(_EFFICIENCY_CALLS, token), (since,)).fetchall()]
        tasks = [dict(r) for r in conn.execute(_EFFICIENCY_TASKS).fetchall()]
    except _DB_ERRORS:
        return {"calls": [], "tasks": []}
    finally:
        conn.close()
    return {"calls": calls, "tasks": tasks}


def _attempts_columns(conn: Any, token: str) -> set[str]:
    """Edge. The column names of `attempts`, asked of the backend: a failed SELECT would abort a Postgres transaction."""
    if token == placeholder("postgres://"):
        sql = "SELECT column_name AS name FROM information_schema.columns WHERE table_name = {p} AND table_schema = current_schema()"
        return {r["name"] for r in conn.execute(_sql(sql, token), ("attempts",)).fetchall()}
    return {r["name"] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}


def attempt_causes(runs_dir: Path, since: str) -> list[dict[str, Any]]:
    """Edge. Each attempt's kind, cause, cause_why, reason and ts at or after `since`, oldest first; empty with no store or an unreadable one.

    `ts` is ISO text, so a `YYYY-MM-DD` `since` compares correctly as text. A store below schema 5 has no `cause`
    or `cause_why` column: both read as None."""
    opened = _open(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    try:
        cols = "cause, cause_why" if "cause" in _attempts_columns(conn, p) else "NULL AS cause, NULL AS cause_why"
        sql = _sql(f"SELECT kind, {cols}, reason, ts FROM attempts WHERE ts >= {{p}} ORDER BY ts", p)
        keys = ("kind", "cause", "cause_why", "reason", "ts")
        return [{k: r[k] for k in keys} for r in conn.execute(sql, (since,)).fetchall()]
    except _DB_ERRORS:
        return []
    finally:
        conn.close()


def attempt_causes_for(runs_dir: Path, run_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Edge. Each attempt's run_id, task_id, seq and cause for the given runs, by run then seq; empty with no store, no runs or an unreadable store.

    A store below schema 5 has no `cause` column: it reads as None."""
    opened = _open(runs_dir) if run_ids else None
    if opened is None:
        return []
    conn, p = opened
    try:
        cols = "cause" if "cause" in _attempts_columns(conn, p) else "NULL AS cause"
        marks = ", ".join(["{p}"] * len(run_ids))
        sql = _sql(f"SELECT run_id, task_id, seq, {cols} FROM attempts WHERE run_id IN ({marks}) ORDER BY run_id, seq", p)
        keys = ("run_id", "task_id", "seq", "cause")
        return [{k: r[k] for k in keys} for r in conn.execute(sql, tuple(run_ids)).fetchall()]
    except _DB_ERRORS:
        return []
    finally:
        conn.close()


def rescue_failures(runs_dir: Path) -> list[dict[str, Any]]:
    """Edge. Each attempts row of kind rescue_failed as run_id, task_id, phase_id, ts and cause, oldest first; empty with no store or an unreadable one.

    A store below schema 5 has no `cause` column: it reads as None."""
    opened = _open(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    try:
        cols = "cause" if "cause" in _attempts_columns(conn, p) else "NULL AS cause"
        sql = _sql(f"SELECT run_id, task_id, phase_id, ts, {cols} FROM attempts WHERE kind = {{p}} ORDER BY ts", p)
        keys = ("run_id", "task_id", "phase_id", "ts", "cause")
        return [{k: r[k] for k in keys} for r in conn.execute(sql, ("rescue_failed",)).fetchall()]
    except _DB_ERRORS:
        return []
    finally:
        conn.close()


def run_spans(runs_dir: Path, since: str) -> list[tuple[str, str, str | None]]:
    """Edge. (run_id, launched_at, ended_at) of runs still open or ended at or after `since`, by launch time; empty with no store or an unreadable one.

    A run with no `ended_at` that is not live (killed, or from before ended_at was stamped) ends at its last
    recorded call, or at its launch when it has none; only a live run stays open."""
    opened = _open(runs_dir)
    if opened is None:
        return []
    conn, p = opened

    def last_call(run_id: str) -> str | None:
        try:
            return conn.execute(_sql("SELECT MAX(ts) AS last FROM node_calls WHERE run_id = {p}", p), (run_id,)).fetchone()["last"]
        except _DB_ERRORS:
            return None

    spans = []
    try:
        rows = conn.execute(
            _sql(
                "SELECT run_id, launched_at, ended_at FROM runs "
                "WHERE launched_at IS NOT NULL AND (ended_at IS NULL OR ended_at >= {p}) ORDER BY launched_at",
                p,
            ),
            (since,),
        ).fetchall()
        for row in rows:
            run_id, launched_at, ended_at = row["run_id"], row["launched_at"], row["ended_at"]
            if ended_at is None and _run_ended(Path(runs_dir), run_id, None):
                ended_at = last_call(run_id) or launched_at
                if ended_at < since:
                    continue
            spans.append((run_id, launched_at, ended_at))
    except _DB_ERRORS:
        return []
    finally:
        conn.close()
    return spans


def _read_file(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _run_ended(runs_dir: Path, run_id: str, ended_at: Any) -> bool:
    """Edge. True when `ended_at` is set, or `<run_id>.pid` is missing, unreadable, not an int, or not a live run."""
    if ended_at is not None:
        return True
    pidfile = Path(runs_dir) / f"{run_id}.pid"
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return True
    return not epic.run_live(pid, pidfile)


def _read_calls(runs_dir: Path, run_id: str) -> list[dict]:
    opened = _open(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    try:
        rows = conn.execute(
            _sql(
                "SELECT n.*, r.ended_at AS run_ended_at FROM node_calls n JOIN runs r ON r.run_id = n.run_id "
                "WHERE n.run_id = {p} ORDER BY n.ts, n.seq",
                p,
            ),
            (run_id,),
        ).fetchall()
    except _DB_ERRORS:
        return []
    finally:
        conn.close()
    if not rows or not _run_ended(runs_dir, run_id, rows[0]["run_ended_at"]):
        return []
    return [call_from_row(r) for r in rows]


def usage(runs_dir: Path, run_id: str) -> dict | None:
    """The usage file unchanged when it parses as an object, else the store's rows for an ended run (`ended_at` set, or no live pid), else None."""
    from_file = _read_file(Path(runs_dir) / f"{run_id}.usage.json")
    if from_file is not None:
        return from_file
    calls = _read_calls(Path(runs_dir), run_id)
    return _store_usage(run_id, calls) if calls else None


def _store_usage(run_id: str, calls: list[dict]) -> dict:
    return {"run_id": run_id, "calls": calls, "summary": summarize(calls)}


def _store_runs(runs_dir: Path, exclude: Collection[str] = (), since: str | None = None) -> dict[str, list[dict]]:
    """Every run with `node_calls` rows and a `runs` row that has ended (`ended_at` set, or no live pid), its calls ordered by ts then seq.
    Runs in `exclude` are dropped before any row becomes a call and before the pid check. With `since`, a run whose `ended_at` is set and earlier is dropped in SQL."""
    opened = _open(runs_dir)
    if opened is None:
        return {}
    conn, p = opened
    where, params = ("WHERE r.ended_at IS NULL OR r.ended_at >= {p} ", (since,)) if since is not None else ("", ())
    try:
        rows = conn.execute(
            _sql(
                "SELECT n.*, r.ended_at AS run_ended_at FROM node_calls n JOIN runs r ON r.run_id = n.run_id "
                + where
                + "ORDER BY n.run_id, n.ts, n.seq",
                p,
            ),
            params,
        ).fetchall()
    except _DB_ERRORS:
        return {}
    finally:
        conn.close()
    skip = frozenset(exclude)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row["run_id"] not in skip:
            grouped.setdefault(row["run_id"], []).append(row)
    return {
        rid: [call_from_row(r) for r in group]
        for rid, group in grouped.items()
        if _run_ended(runs_dir, rid, group[0]["run_ended_at"])
    }


def store_usages(runs_dir: Path, exclude: Collection[str] = (), since: str | None = None) -> dict[str, dict]:
    """Run id to usage for each ended store run not in `exclude`. `since` is an ISO timestamp: a run that ended before it is left out."""
    return {rid: _store_usage(rid, calls) for rid, calls in _store_runs(Path(runs_dir), exclude, since).items()}


def usages(runs_dir: Path) -> dict[str, dict]:
    """Run id to usage: every parsing usage file, then each ended store run (`ended_at` set, or no live pid) that has no file."""
    files = {
        path.name.removesuffix(".usage.json"): body
        for path in sorted(Path(runs_dir).glob("*.usage.json"))
        if (body := _read_file(path)) is not None
    }
    return {**files, **store_usages(runs_dir, exclude=files)}


def _manifest_files(runs_dir: Path) -> dict[str, list[dict]]:
    """Run id to its parsing `<run_id>:<phase>.json` manifests, sorted by file name. The phase follows the last colon."""
    named = [
        (path.name.removesuffix(".json").rpartition(":")[0], path)
        for path in sorted(Path(runs_dir).glob("*:*.json"))
        if not path.name.endswith(".usage.json")
    ]
    by_run: dict[str, list[dict]] = {}
    for run_id, path in named:
        if (body := _read_file(path)) is not None:
            by_run.setdefault(run_id, []).append(body)
    return by_run


def _manifest_record(record_json: Any) -> dict | None:
    record = _json_cell(record_json)
    found = record.get("manifest_record") if isinstance(record, dict) else None
    return found if isinstance(found, dict) else None


def _store_manifests(runs_dir: Path) -> dict[str, list[dict]]:
    """Run id to the `manifest_record` of each of its `phases` rows, ended run or not, ordered by the row's ts."""
    opened = _open(runs_dir)
    if opened is None:
        return {}
    conn, _ = opened
    try:
        rows = conn.execute("SELECT run_id, record_json FROM phases ORDER BY run_id, ts").fetchall()
    except _DB_ERRORS:
        return {}
    finally:
        conn.close()
    by_run: dict[str, list[dict]] = {}
    for row in rows:
        if (manifest := _manifest_record(row["record_json"])) is not None:
            by_run.setdefault(row["run_id"], []).append(manifest)
    return by_run


def phase_manifests(runs_dir: Path, run_id: str) -> list[dict]:
    """The run's manifest files when any parse, else the `manifest_record` of its store phases, else []."""
    from_files = _manifest_files(Path(runs_dir)).get(run_id)
    return from_files or _store_manifests(Path(runs_dir)).get(run_id, [])


def all_phase_manifests(runs_dir: Path) -> dict[str, list[dict]]:
    """Run id to phase manifests: every run with manifest files, then each store run that has none."""
    files = _manifest_files(Path(runs_dir))
    stored = {rid: ms for rid, ms in _store_manifests(Path(runs_dir)).items() if rid not in files}
    return {**files, **stored}


def _store_phase_names(runs_dir: Path, run_id: str) -> list[str]:
    opened = _open(runs_dir)
    if opened is None:
        return []
    conn, p = opened
    try:
        rows = conn.execute(_sql("SELECT phase_id FROM phases WHERE run_id = {p} ORDER BY ts", p), (run_id,)).fetchall()
    except _DB_ERRORS:
        return []
    finally:
        conn.close()
    return [row["phase_id"] for row in rows]


def phase_names(runs_dir: Path, run_id: str) -> list[str]:
    """Phase names of the run's manifest files, oldest mtime first; else its store phases by ts, ended run or not."""
    paths = sorted(Path(runs_dir).glob(f"{run_id}:*.json"), key=lambda p: p.stat().st_mtime)
    matches = (_PHASE_FILE.match(p.name) for p in paths)
    return [m.group(1) for m in matches if m] or _store_phase_names(Path(runs_dir), run_id)


def _json_objects(lines: Any) -> list[dict]:
    """The lines that parse as a JSON object, in order; blank, unparseable and non-object lines are dropped."""
    def parsed(line: str) -> Any:
        try:
            return json.loads(line)
        except ValueError:
            return None

    return [row for line in lines if line.strip() if isinstance(row := parsed(line), dict)]


def _loose_events(path: Path) -> list[dict]:
    try:
        return _json_objects(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return []


def _store_events(traces: Path, run_id: str, call_id: str) -> list[dict]:
    """Events of one call from `YYYY/MM/DD/<run_id>.jsonl.zst` day files, ordered by each row's `seq`."""
    try:
        import zstandard
    except ImportError as exc:
        raise TracesUnavailable("reading traces needs zstandard: install coxswain-tools[traces]") from exc

    def rows(path: Path) -> list[dict]:
        with path.open("rb") as fh:
            reader = zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
            return _json_objects(list(io.TextIOWrapper(reader, encoding="utf-8")))

    mine = [r for path in sorted(traces.glob(f"*/*/*/{run_id}.jsonl.zst")) for r in rows(path) if r.get("call_id") == call_id]
    return [r["event"] for r in sorted(mine, key=lambda r: r["seq"])]


_PARQUET_COLUMNS = ["call_id", "seq", "event"]
_NEEDS_PYARROW = "reading Parquet traces needs pyarrow: pip install 'coxswain-tools[parquet]'"


def _parquet_call_events(rows: Sequence[Mapping[str, Any]], call_id: str) -> list[dict]:
    """Pure: the decoded `event` of each row for `call_id`, ordered by `seq`; an event that is not a JSON object is dropped."""
    mine = sorted((r for r in rows if r.get("call_id") == call_id), key=lambda r: r["seq"])
    return _json_objects(r["event"] for r in mine if isinstance(r.get("event"), str))


def _dump_rows(stdout: str) -> list[dict]:
    """Pure: the rows of `harness.store_traces dump` output, one JSON object per line, cut to `_PARQUET_COLUMNS`."""
    return [{c: row[c] for c in _PARQUET_COLUMNS} for row in map(json.loads, filter(str.strip, stdout.splitlines()))]


_TASK_RECORD_SCRIPT = """import json, sqlite3, sys
url, *ids = sys.argv[1:]
sql = "SELECT record_json FROM task_records WHERE run_id = {0} AND phase_id = {0} AND task_id = {0}"
if url.startswith(("postgres:", "postgresql:")):
    import psycopg
    conn, mark = psycopg.connect(url), "%s"
else:
    path = url.removeprefix("sqlite:///")
    conn, mark = sqlite3.connect("file:" + path + "?mode=ro", uri=True), "?"
row = conn.execute(sql.format(mark), ids).fetchone()
if row:
    print(row[0] if isinstance(row[0], str) else json.dumps(row[0]))
"""


def _task_record_argv(python: str, url: str, run_id: str, phase_id: str, task_id: str) -> list[str]:
    """Pure: the argv that prints one task record's `record_json`. The ids are bound parameters, never SQL text."""
    return [python, "-c", _TASK_RECORD_SCRIPT, url, run_id, phase_id, task_id]


def _task_record_from(stdout: str) -> dict | None:
    """Pure: the record dict in the output, None when it is empty, not JSON, or not a JSON object."""
    try:
        record = json.loads(stdout.strip())
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def task_record(runs_dir: Path, run_id: str, phase_id: str, task_id: str) -> dict | None:
    """Edge: one task's record from the store through the harness python, None when it cannot say (caller falls back to the file)."""
    python = _harness_python()
    if python is None:
        return None
    argv = _task_record_argv(str(python), _store_url(Path(runs_dir)), run_id, phase_id, task_id)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return _task_record_from(done.stdout) if done.returncode == 0 else None


_TASK_RECORD_PHASES_SCRIPT = """import sqlite3, sys
url, *ids = sys.argv[1:]
sql = "SELECT DISTINCT phase_id FROM task_records WHERE run_id = {0} AND task_id = {0} ORDER BY phase_id"
if url.startswith(("postgres:", "postgresql:")):
    import psycopg
    conn, mark = psycopg.connect(url), "%s"
else:
    path = url.removeprefix("sqlite:///")
    conn, mark = sqlite3.connect("file:" + path + "?mode=ro", uri=True), "?"
for row in conn.execute(sql.format(mark), ids).fetchall():
    print(row[0])
"""


def _task_record_phases_argv(python: str, url: str, run_id: str, task_id: str) -> list[str]:
    """Pure: the argv that prints each phase holding a record for the run and task. The ids are bound parameters."""
    return [python, "-c", _TASK_RECORD_PHASES_SCRIPT, url, run_id, task_id]


def _phases_from(stdout: str) -> list[str]:
    """Pure: the non-blank lines of the output, in order."""
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def task_record_phases(runs_dir: Path, run_id: str, task: str) -> list[str]:
    """Edge: the distinct phases whose `task_records` rows name this run and task; empty with no store or on a failed read."""
    python = _harness_python()
    if python is None:
        return []
    argv = _task_record_phases_argv(str(python), _store_url(Path(runs_dir)), run_id, task)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return _phases_from(done.stdout) if done.returncode == 0 else []


_RUN_TASK_IDS_SCRIPT = """import sqlite3, sys
url, *ids = sys.argv[1:]
sql = "SELECT DISTINCT task_id FROM task_records WHERE run_id = {0} ORDER BY task_id"
if url.startswith(("postgres:", "postgresql:")):
    import psycopg
    conn, mark = psycopg.connect(url), "%s"
else:
    path = url.removeprefix("sqlite:///")
    conn, mark = sqlite3.connect("file:" + path + "?mode=ro", uri=True), "?"
for row in conn.execute(sql.format(mark), ids).fetchall():
    print(row[0])
"""


def _run_task_ids_argv(python: str, url: str, run_id: str) -> list[str]:
    """Pure: the argv that prints each task id holding a record for the run. The run id is a bound parameter."""
    return [python, "-c", _RUN_TASK_IDS_SCRIPT, url, run_id]


def run_task_ids(runs_dir: Path, run_id: str) -> list[str]:
    """Edge: the distinct task ids whose `task_records` rows name this run; empty with no store or on a failed read."""
    python = _harness_python()
    if python is None:
        return []
    argv = _run_task_ids_argv(str(python), _store_url(Path(runs_dir)), run_id)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return _phases_from(done.stdout) if done.returncode == 0 else []


_WORK_ITEMS_COLUMNS = ("initiative", "task_id", "phase", "state", "needs_json", "updated_at", "updated_by")

_WORK_ITEMS_SCRIPT = """import json, sqlite3, sys
url, *want = sys.argv[1:]
sql = "SELECT {columns} FROM work_items" + (" WHERE initiative = {0}" if want else "")
if url.startswith(("postgres:", "postgresql:")):
    import psycopg
    conn, mark = psycopg.connect(url), "%s"
else:
    path = url.removeprefix("sqlite:///")
    conn, mark = sqlite3.connect("file:" + path + "?mode=ro", uri=True), "?"
cur = conn.execute(sql.format(mark), want)
names = [d[0] for d in cur.description]
for row in cur.fetchall():
    print(json.dumps(dict(zip(names, row)), default=str))
""".replace("{columns}", ", ".join(_WORK_ITEMS_COLUMNS))


def _work_items_argv(python: str, url: str, initiative: str | None) -> list[str]:
    """Pure: the argv that prints `work_items` rows as JSON lines. The initiative is a bound argument, never SQL text."""
    return [python, "-c", _WORK_ITEMS_SCRIPT, url, *([] if initiative is None else [initiative])]


def _work_items_from(stdout: str) -> list[dict]:
    """Pure: the JSON-object lines of the output whole, so extra columns pass through. Anything else is skipped."""
    parsed = map(_task_record_from, stdout.splitlines())
    return [row for row in parsed if row is not None]


def task_state(rows: list[dict], initiative: str, task: str) -> str | None:
    """Pure: the `state` of the row for this initiative and task, or None when no row matches."""
    return next((row.get("state") for row in rows if row.get("initiative") == initiative and row.get("task_id") == task), None)


def work_items(runs_dir: Path, initiative: str | None = None) -> list[dict]:
    """Edge: `work_items` rows through the harness python, optionally for one initiative.

    Empty when the table, the harness or the store is missing or unreadable, so an old store reads as an empty one."""
    python = _harness_python()
    if python is None:
        return []
    argv = _work_items_argv(str(python), _store_url(Path(runs_dir)), initiative)
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return _work_items_from(done.stdout) if done.returncode == 0 else []


def task_state_of(runs_dir: Path, initiative: str, task: str) -> str | None:
    """Edge: one task's state from the store, None when the reader finds nothing, so it never raises."""
    return task_state(work_items(runs_dir, initiative), initiative, task)


_QUEUE_COLUMNS = ("kind", "initiative", "task_id", "phase", "state", "needs", "title", "surfaces", "body", "extra")
_CLAIM_KEYS = ("holder", "epoch", "expires_at")
_HELD_EXIT = 4
HELD = "held"
UNAVAILABLE = "unavailable"

# Assumed harness subcommands, for graphs to match. Each is `<python> -m harness.store_queue <verb> <store-url> ...`:
#   read URL [--initiative I] [--kind K]       one JSON object per row per line, extra columns allowed
#   upsert URL ROW_JSON                        one row keyed (initiative, task_id); exit 0 when written, safe to repeat
#   claim URL INITIATIVE TASK HOLDER TTL_S NOW one conditional row update that bumps epoch; exit 0 prints the
#                                              claim JSON {holder, epoch, expires_at}; exit 4 means a live claim by
#                                              another holder (unknown: graphs must confirm 4 for held)
#   release URL INITIATIVE TASK HOLDER         clears the claim only when HOLDER matches; exit 0 when cleared
# Any other non-zero exit, or a harness that is absent, reads as unavailable.


def _queue_read_argv(python: str, url: str, initiative: str | None, kind: str | None) -> list[str]:
    """Pure: the argv that prints queue rows as JSON lines, narrowed by initiative and kind when given."""
    narrow = [*([] if initiative is None else ["--initiative", initiative]), *([] if kind is None else ["--kind", kind])]
    return [python, "-m", "harness.store_queue", "read", url, *narrow]


def _queue_upsert_argv(python: str, url: str, row: Mapping[str, Any]) -> list[str]:
    """Pure: the argv that upserts one row. The row is one JSON argument with sorted keys, so equal rows give equal argv."""
    return [python, "-m", "harness.store_queue", "upsert", url, json.dumps(row, sort_keys=True, default=str)]


def _queue_claim_argv(python: str, url: str, initiative: str, task_id: str, holder: str, ttl_s: float, now: str) -> list[str]:
    """Pure: the argv that claims a row. `now` is the caller's clock reading, so the store never consults its own."""
    return [python, "-m", "harness.store_queue", "claim", url, initiative, task_id, holder, str(ttl_s), now]


def _queue_release_argv(python: str, url: str, initiative: str, task_id: str, holder: str) -> list[str]:
    """Pure: the argv that clears a row's claim when `holder` holds it."""
    return [python, "-m", "harness.store_queue", "release", url, initiative, task_id, holder]


def _queue_rows_from(stdout: str) -> list[dict]:
    """Pure: the JSON-object lines of the output, each with every queue and claim key present (None when absent) and extras kept."""
    blank = dict.fromkeys((*_QUEUE_COLUMNS, *_CLAIM_KEYS))
    return [{**blank, **row} for row in _work_items_from(stdout)]


def _claim_from(stdout: str) -> dict | None:
    """Pure: `{holder, epoch, expires_at}` from a JSON object naming a holder, else None."""
    claim = _task_record_from(stdout)
    return None if claim is None or claim.get("holder") is None else {key: claim.get(key) for key in _CLAIM_KEYS}


def _claim_reason(returncode: int) -> str:
    """Pure: why a non-zero claim exit failed, `HELD` for the assumed held code, else `UNAVAILABLE`."""
    return HELD if returncode == _HELD_EXIT else UNAVAILABLE


def _queue_run(runs_dir: Path, build: Any) -> Any | None:
    """Edge: run the argv `build(python, url)` through the harness python; None when it cannot run. Never raises."""
    python = _harness_python()
    if python is None:
        return None
    try:
        argv = build(str(python), _store_url(Path(runs_dir)))
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except Exception:  # an unreadable profile or store must read as unavailable, not crash the caller
        return None


def read_queue(runs_dir: Path, initiative: str | None = None, kind: str | None = None) -> list[dict]:
    """Edge: queue rows from the store, optionally for one initiative and kind. Empty when the harness, table or store is unavailable."""
    done = _queue_run(runs_dir, lambda python, url: _queue_read_argv(python, url, initiative, kind))
    return _queue_rows_from(done.stdout) if done is not None and done.returncode == 0 else []


def upsert_row(runs_dir: Path, row: Mapping[str, Any]) -> bool:
    """Edge: True when the store took the row, False when it could not."""
    done = _queue_run(runs_dir, lambda python, url: _queue_upsert_argv(python, url, row))
    return done is not None and done.returncode == 0


def upsert_row_detail(runs_dir: Path, row: Mapping[str, Any]) -> str:
    """Edge: "" only when the store took the row; otherwise a non-empty reason: the command's last
    line of stderr, `exit N` when it failed silently, or "harness python not found"."""
    done = _queue_run(runs_dir, lambda python, url: _queue_upsert_argv(python, url, row))
    if done is None:
        return "harness python not found"
    if done.returncode == 0:
        return ""
    lines = done.stderr.strip().splitlines()
    return lines[-1] if lines else f"exit {done.returncode}"


def release_row(runs_dir: Path, initiative: str, task_id: str, holder: str) -> bool:
    """Edge: True when the claim was cleared, False when `holder` did not hold it or the store could not say."""
    done = _queue_run(runs_dir, lambda python, url: _queue_release_argv(python, url, initiative, task_id, holder))
    return done is not None and done.returncode == 0


def claim_outcome(runs_dir: Path, initiative: str, task_id: str, holder: str, ttl_s: float, now: str) -> tuple[dict | None, str | None]:
    """Edge: `(claim, None)` when taken, else `(None, HELD)` or `(None, UNAVAILABLE)` so a caller can tell them apart."""
    done = _queue_run(runs_dir, lambda python, url: _queue_claim_argv(python, url, initiative, task_id, holder, ttl_s, now))
    if done is None:
        return None, UNAVAILABLE
    if done.returncode != 0:
        return None, _claim_reason(done.returncode)
    claim = _claim_from(done.stdout)
    return (claim, None) if claim is not None else (None, UNAVAILABLE)


def claim_row(runs_dir: Path, initiative: str, task_id: str, holder: str, ttl_s: float, now: str) -> dict | None:
    """Edge: the claim taken on a free or expired row at `now`, None when held or unavailable; `claim_outcome` says which."""
    return claim_outcome(runs_dir, initiative, task_id, holder, ttl_s, now)[0]


def _import_pyarrow() -> tuple[Any, Any]:
    """The only place pyarrow is imported: `(pyarrow.fs, pyarrow.parquet)`, else TracesUnavailable naming the extra."""
    try:
        import pyarrow.fs
        import pyarrow.parquet
    except ImportError as exc:
        raise TracesUnavailable(_NEEDS_PYARROW) from exc
    return pyarrow.fs, pyarrow.parquet


class ObjectStoreMisconfigured(TracesUnavailable):
    """The provider profile's object_store block cannot build a filesystem: a literal secret, or an env var that is not set."""


def s3_options(block: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """`S3FileSystem` keyword arguments for the profile's `object_store` block, credentials read from `env`.

    `FileSystem.from_uri` knows nothing of the block, so a private endpoint is unreachable without this.
    A literal secret is refused. An endpoint with no scheme is https; path_style defaults to true."""
    literal = sorted(_LITERAL_SECRETS.intersection(block))
    if literal:
        raise ValueError(
            f"object_store holds a literal secret ({', '.join(literal)}); name an env var in access_key_env or secret_key_env"
        )
    endpoint = str(block.get("endpoint") or "")
    scheme, _, host = endpoint.partition("://") if "://" in endpoint else ("https", "", endpoint)
    optional = {
        **({"endpoint_override": host} if host else {}),
        **({"region": str(block["region"])} if block.get("region") else {}),
        **({"access_key": _from_env(block["access_key_env"], env)} if block.get("access_key_env") else {}),
        **({"secret_key": _from_env(block["secret_key_env"], env)} if block.get("secret_key_env") else {}),
    }
    return {"scheme": scheme.lower(), **optional, "force_virtual_addressing": not block.get("path_style", True)}


def _block_options(root: TracesRoot) -> dict[str, Any] | None:
    """Edge: `s3_options` for an s3:// root with an object_store block, else None. A bad block raises ObjectStoreMisconfigured."""
    if not (root.remote and root.object_store and root.url.lower().startswith("s3://")):
        return None
    try:
        return s3_options(root.object_store, os.environ)
    except ValueError as exc:
        raise ObjectStoreMisconfigured(str(exc)) from exc


def _filesystem(pafs: Any, root: TracesRoot) -> tuple[Any, str]:
    """pyarrow's own resolution for a remote URL, or an S3FileSystem from the `object_store` block for an s3:// URL that has one;
    a plain local path (possibly relative) gets the local filesystem."""
    options = _block_options(root)
    if options is not None:
        return pafs.S3FileSystem(**options), root.url[len("s3://"):].strip("/")
    if root.remote:
        return pafs.FileSystem.from_uri(root.url)
    return pafs.LocalFileSystem(), str(Path(root.url).absolute())


def _dump_failure(returncode: int, stderr: str) -> str:
    """Pure: the exit code and the last stderr line of a failed dump, for the TracesUnavailable message."""
    last = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "")
    return f"exit {returncode}: {last}" if last else f"exit {returncode}"


@functools.lru_cache(maxsize=8)
def _harness_dump(python: str, url: str, run_id: str) -> tuple[dict, ...] | str:
    """Edge, cached per process: the run's dumped rows, else the failure as text, so a broken harness runs once.

    Exit 3 raises LookupError, which lru_cache does not store: a live run's file can appear later."""
    argv = [python, "-m", "harness.store_traces", "dump", url, run_id]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "timed out after 60s"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if done.returncode == 3:
        raise LookupError(run_id)
    if done.returncode != 0:
        return _dump_failure(done.returncode, done.stderr)
    try:
        return tuple(_dump_rows(done.stdout))
    except (ValueError, KeyError, TypeError) as exc:
        return f"unreadable output: {type(exc).__name__}: {exc}"


def _harness_parquet_rows(root: TracesRoot, run_id: str, missing: TracesUnavailable) -> list[dict] | None:
    """Edge: the run's rows through the harness `store_traces dump`, None on exit 3, else `missing` re-raised."""
    python = _harness_python()
    if python is None:
        raise missing
    try:
        dumped = _harness_dump(str(python), root.url, run_id)
    except LookupError:
        return None
    if isinstance(dumped, str):
        raise TracesUnavailable(f"{missing} (the harness could not read it either): {dumped}") from missing
    return list(dumped)


def _harness_can_read(python: Path) -> bool:
    """Edge: whether the harness python imports pyarrow and `harness.store_traces`, the two things a dump needs."""
    argv = [str(python), "-c", "import pyarrow.parquet, harness.store_traces"]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _parquet_rows(root: TracesRoot, run_id: str) -> list[dict] | None:
    """Edge: the rows of the run's `YYYY/MM/DD/<run_id>.parquet` files, None when it has none.

    A local root is probed with a glob first, so a missing pyarrow only matters once a file exists. A remote root
    cannot be probed without pyarrow. Without pyarrow the rows come through the harness when it has a python,
    else TracesUnavailable is raised."""
    name = f"{run_id}.parquet"
    if root.remote:
        try:
            pafs, pq = _import_pyarrow()
        except TracesUnavailable as missing:
            return _harness_parquet_rows(root, run_id, missing)
        fs, base = _filesystem(pafs, root)
        selector = pafs.FileSelector(base, recursive=True, allow_not_found=True)
        found = sorted(i.path for i in fs.get_file_info(selector) if i.type == pafs.FileType.File and i.base_name == name)
    else:
        found = sorted(str(p) for p in Path(root.url).absolute().glob(f"*/*/*/{name}"))
        if not found:
            return None
        try:
            pafs, pq = _import_pyarrow()
        except TracesUnavailable as missing:
            return _harness_parquet_rows(root, run_id, missing)
        fs, _ = _filesystem(pafs, root)
    if not found:
        return None
    return [row for path in found for row in pq.read_table(path, filesystem=fs, columns=_PARQUET_COLUMNS).to_pylist()]


@functools.lru_cache(maxsize=8)
def _found_parquet_rows(url: str, remote: bool, block: str, run_id: str) -> tuple[dict, ...]:
    """Raises LookupError on a miss, which lru_cache does not store: a live run's file can appear later.
    `block` is the object_store block as sorted JSON, since a mapping is not hashable; "" for none."""
    rows = _parquet_rows(TracesRoot(url, remote, json.loads(block) if block else None), run_id)
    if not rows:
        raise LookupError(run_id)
    return tuple(rows)


def _parquet_rows_once(root: TracesRoot, run_id: str) -> tuple[dict, ...] | None:
    """Edge: `_parquet_rows` read once per process for a run whose file exists; a run with none is asked again."""
    try:
        block = json.dumps(root.object_store, sort_keys=True, default=str) if root.object_store else ""
        return _found_parquet_rows(root.url, root.remote, block, run_id)
    except LookupError:
        return None


def synthetic_call_id(run_id: str, trace: object) -> str | None:
    """Pure: the id the Parquet backfill gave a call, `<run_id>-<stem of its trace path>`; None without a trace."""
    return f"{run_id}-{Path(trace).stem}" if isinstance(trace, str) and trace else None


@functools.lru_cache(maxsize=8)
def _store_ids_by_trace(runs_dir: str, run_id: str) -> dict[str, str]:
    """Edge, cached per run: the store's call id for each trace path its node_calls rows name. Calls from before call
    ids carry `legacy:<run>:<seq>` in the store, and the Parquet traces are relinked to it (graphs #445)."""
    opened = _open(Path(runs_dir))
    if opened is None:
        return {}
    conn, p = opened
    try:
        rows = conn.execute(_sql("SELECT call_id, detail_json FROM node_calls WHERE run_id = {p}", p), (run_id,)).fetchall()
    except _DB_ERRORS:
        return {}
    finally:
        conn.close()
    ids = {}
    for row in rows:
        detail = _json_cell(row["detail_json"])
        trace = detail.get("trace") if isinstance(detail, dict) else None
        if isinstance(trace, str) and trace:
            ids[trace] = row["call_id"]
    return ids


def call_events(runs_dir: Path, run_id: str, call: Mapping[str, Any]) -> list[dict] | None:
    """A call's stream events by `call["id"]`, first source holding any wins: the run's Parquet file (for a call with
    no id, under the store's id for its trace path, then the backfill's synthetic id), then the `.jsonl.zst` day
    files, then the loose `trace` file. None when there is no trace store and no loose file."""
    call_id = str(call.get("id"))
    trace = call.get("trace")
    synthetic = synthetic_call_id(run_id, trace)
    rows, fault = _parquet_rows_or_fault(_traces_root(Path(runs_dir)), run_id)
    if rows and (
        found := _parquet_call_events(rows, call_id)
        or (isinstance(trace, str) and trace and _parquet_call_events(rows, _store_ids_by_trace(str(runs_dir), run_id).get(trace, "")))
        or (synthetic and _parquet_call_events(rows, synthetic))
    ):
        return found
    traces = Path(runs_dir) / TRACES_DIRNAME
    stored = _store_events(traces, run_id, call_id) if traces.is_dir() else None
    if stored:
        return stored
    loose = call.get("trace")
    if isinstance(loose, str) and loose and Path(loose).is_file():
        return _loose_events(Path(loose))
    if fault is not None:
        raise fault
    return stored


def _parquet_rows_or_fault(root: TracesRoot, run_id: str) -> tuple[tuple[dict, ...] | None, ObjectStoreMisconfigured | None]:
    """Edge: `_parquet_rows_once`, with a bad object_store block returned rather than raised, so local sources are still tried."""
    try:
        return _parquet_rows_once(root, run_id), None
    except ObjectStoreMisconfigured as fault:
        return None, fault


@dataclass(frozen=True)
class ParquetCheck:
    """`readable` with the `reason`: `ok`, `through the harness`, `pyarrow missing`, `root unreachable`, or the
    object_store error text, which names the literal secret or the unset env var."""

    readable: bool
    reason: str


def parquet_readable(traces_root: TracesRoot, harness: Path | None) -> ParquetCheck:
    """Edge: whether Parquet traces under `traces_root` can be read, for the doctor. Opens no trace file.

    `harness` is the diagnosed profile's harness python; without pyarrow it counts only when it imports what a dump needs.
    A bad object_store block is reported first, by its own message, since no filesystem can be built from it."""
    try:
        _block_options(traces_root)
    except ObjectStoreMisconfigured as exc:
        return ParquetCheck(False, str(exc))
    try:
        pafs, _ = _import_pyarrow()
    except TracesUnavailable:
        readable = harness is not None and _harness_can_read(harness)
        return ParquetCheck(True, "through the harness") if readable else ParquetCheck(False, "pyarrow missing")
    import pyarrow

    try:
        fs, base = _filesystem(pafs, traces_root)
        reachable = fs.get_file_info(base).type == pafs.FileType.Directory
    except (OSError, ValueError, pyarrow.ArrowException):
        reachable = False
    return ParquetCheck(True, "ok") if reachable else ParquetCheck(False, "root unreachable")


def run_started(runs_dir: Path, run_id: str) -> str | None:
    """The `launched_at` of the store's `runs` row, else None."""
    opened = _open(Path(runs_dir))
    if opened is None:
        return None
    conn, p = opened
    try:
        row = conn.execute(_sql("SELECT launched_at FROM runs WHERE run_id = {p}", p), (run_id,)).fetchone()
    except _DB_ERRORS:
        return None
    finally:
        conn.close()
    return None if row is None else row["launched_at"]
