"""Each run_store reader returns the same value from a SQLite store and from a Postgres store.

The Postgres cases run only when COX_TEST_POSTGRES_URL names a database the test may create a schema in.
"""

import os
import sqlite3
import uuid
from pathlib import Path

import pytest
import yaml

from agent_tools import run_store
from agent_tools.store_dialect import placeholder

POSTGRES_URL = os.environ.get("COX_TEST_POSTGRES_URL")

# Portable DDL: TEXT for every timestamp, DOUBLE PRECISION (REAL is float4 on Postgres), BOOLEAN for ok.
SEED_DDL = (
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, launched_at TEXT, ended_at TEXT)",
    "CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, expires_at TEXT, heartbeat_at TEXT)",
    "CREATE TABLE phases (run_id TEXT, phase_id TEXT, ts TEXT, record_json TEXT)",
    "CREATE TABLE node_calls (call_id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, task_id TEXT, role TEXT, tier TEXT, "
    "model_alias TEXT, cost_usd DOUBLE PRECISION, ceiling_usd DOUBLE PRECISION, ceiling_source TEXT, turns INTEGER, "
    "duration_ms INTEGER, input_tokens INTEGER, cache_read_tokens INTEGER, cache_creation_tokens INTEGER, "
    "input_total INTEGER, output_tokens INTEGER, ok BOOLEAN, ts TEXT, decision_json TEXT, detail_json TEXT)",
)

T1, T2 = "2026-09-25T04:10:00+00:00", "2026-09-25T04:20:00+00:00"
R1_ENDED, R2_ENDED = "2026-09-25T05:00:00+00:00", "2026-09-20T05:00:00+00:00"
SINCE = "2026-09-24T00:00:00+00:00"
LEASE = ("h1", "2026-09-25T06:00:00+00:00", "2026-09-25T05:59:00+00:00")


def _call(call_id, run_id, seq, role, tier, model, cost, turns, tokens, ts, detail=None):
    return {
        "call_id": call_id, "run_id": run_id, "seq": seq, "role": role, "tier": tier, "model_alias": model,
        "cost_usd": cost, "turns": turns, "duration_ms": 100, "input_tokens": tokens, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "input_total": tokens * 2, "output_tokens": tokens // 2, "ok": True, "ts": ts,
        "detail_json": detail,
    }


SEED_ROWS = (
    ("runs", {"run_id": "r1", "launched_at": "2026-09-25T04:00:00+00:00", "ended_at": R1_ENDED}),
    ("runs", {"run_id": "r2", "launched_at": "2026-09-20T04:00:00+00:00", "ended_at": R2_ENDED}),
    ("leases", {"name": "runs:r", "holder": LEASE[0], "expires_at": LEASE[1], "heartbeat_at": LEASE[2]}),
    ("phases", {"run_id": "r1", "phase_id": "build", "ts": T2, "record_json": '{"manifest_record": {"phase": "build"}}'}),
    ("phases", {"run_id": "r1", "phase_id": "scope", "ts": T1, "record_json": '{"manifest_record": {"phase": "scope"}}'}),
    ("node_calls", _call("c1", "r1", 1, "scope", "cheap", "haiku", 0.25, 2, 10, T1, '{"summary": "did it"}')),
    ("node_calls", _call("c2", "r1", 2, "build", "mid", "sonnet", 0.5, 1, 14, T2)),
    ("node_calls", _call("c3", "r2", 1, "scope", "cheap", "haiku", 1.0, 4, 20, "2026-09-20T04:10:00+00:00")),
)


def _seed(conn, token: str) -> None:
    for ddl in SEED_DDL:
        conn.execute(ddl)
    for table, row in SEED_ROWS:
        conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join([token] * len(row))})", tuple(row.values()))
    conn.commit()


def _point_at(tmp_path: Path, monkeypatch, storage_url: str | None) -> None:
    """Route run_store's URL resolution to `storage_url`, or to no profile at all so it falls back to cox.db."""
    provider = tmp_path / "provider.yaml"
    provider.write_text(yaml.safe_dump({"storage_url": storage_url} if storage_url else {}))
    routing = tmp_path / "profile.yaml"
    routing.write_text(yaml.safe_dump({"provider_profile": str(provider)}))
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(routing))


@pytest.fixture
def sqlite_dir(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch, None)
    return tmp_path


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.skipif(not POSTGRES_URL, reason="COX_TEST_POSTGRES_URL is not set")),
    ]
)
def runs_dir(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        _point_at(tmp_path, monkeypatch, None)
        conn = sqlite3.connect(tmp_path / "cox.db")
        _seed(conn, placeholder(f"sqlite:///{tmp_path / 'cox.db'}"))
        conn.close()
        yield tmp_path
        return
    psycopg = pytest.importorskip("psycopg")
    schema = f"cox_test_{uuid.uuid4().hex[:12]}"
    scoped = f"{POSTGRES_URL}{'&' if '?' in POSTGRES_URL else '?'}options=-csearch_path%3D{schema}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        try:
            with psycopg.connect(scoped) as conn:
                _seed(conn, placeholder(scoped))
            _point_at(tmp_path, monkeypatch, scoped)
            yield tmp_path
        finally:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


R1_FIRST_CALL = {
    "role": "scope", "task_id": None, "tier": "cheap", "cost_usd": 0.25, "ceiling_usd": None, "ceiling_source": None,
    "turns": 2, "duration_ms": 100, "input_tokens": 10, "cache_read_tokens": 0, "cache_creation_tokens": 0,
    "input_total": 20, "output_tokens": 5, "ts": T1, "id": "c1", "model": "haiku", "model_id": None, "ok": True,
    "decision": None, "summary": "did it",
}
R1_SUMMARY = {
    "calls": 2, "cost_usd": 0.75, "turns": 3, "input_total": 48, "input_tokens": 24, "cache_read_tokens": 0,
    "cache_creation_tokens": 0, "output_tokens": 12,
    "by_model": {
        "haiku": {"calls": 1, "cost_usd": 0.25, "input_total": 20, "input_tokens": 10, "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 5},
        "sonnet": {"calls": 1, "cost_usd": 0.5, "input_total": 28, "input_tokens": 14, "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 7},
    },
}


def test_connect_readonly_opens_a_store_whose_rows_answer_to_column_names(runs_dir):
    conn = run_store.connect_readonly(runs_dir)
    row = conn.execute("SELECT run_id FROM runs ORDER BY run_id").fetchone()
    conn.close()
    assert row["run_id"] == "r1"


def test_lease_reads_the_prefix_lease(runs_dir):
    assert run_store.lease(runs_dir, "r-3") == LEASE


def test_run_ids_lists_every_run(runs_dir):
    assert run_store.run_ids(runs_dir) == {"r1", "r2"}


def test_run_started_reads_launched_at(runs_dir):
    assert run_store.run_started(runs_dir, "r1") == "2026-09-25T04:00:00+00:00"


def test_phase_names_come_back_in_ts_order(runs_dir):
    assert run_store.phase_names(runs_dir, "r1") == ["scope", "build"]


def test_phase_manifests_come_back_in_ts_order(runs_dir):
    assert run_store.phase_manifests(runs_dir, "r1") == [{"phase": "scope"}, {"phase": "build"}]


def test_all_phase_manifests_groups_by_run(runs_dir):
    assert run_store.all_phase_manifests(runs_dir) == {"r1": [{"phase": "scope"}, {"phase": "build"}]}


def test_usage_returns_the_calls_and_summary_of_an_ended_run(runs_dir):
    got = run_store.usage(runs_dir, "r1")
    assert got["run_id"] == "r1"
    assert got["calls"][0] == R1_FIRST_CALL
    assert [c["id"] for c in got["calls"]] == ["c1", "c2"]
    assert got["summary"] == R1_SUMMARY


def test_usages_lists_every_ended_store_run(runs_dir):
    assert list(run_store.usages(runs_dir)) == ["r1", "r2"]


def test_store_usages_since_drops_a_run_that_ended_before_it(runs_dir):
    assert list(run_store.store_usages(runs_dir, since=SINCE)) == ["r1"]


def test_run_spans_keeps_the_runs_ended_since(runs_dir):
    assert run_store.run_spans(runs_dir, SINCE) == [("r1", "2026-09-25T04:00:00+00:00", R1_ENDED)]


def test_a_runs_dir_with_no_store_gives_none_and_creates_nothing(sqlite_dir):
    assert run_store.connect_readonly(sqlite_dir) is None
    assert not (sqlite_dir / "cox.db").exists()


def test_efficiency_rows_sum_calls_by_day_and_task_from_the_cutoff(runs_dir):
    rows = run_store.efficiency_rows(runs_dir, "2026-09-24")
    assert rows["calls"] == [
        {"day": "2026-09-25", "task_id": None, "cost_usd": 0.75, "turns": 3, "cache_read_tokens": 0, "input_total": 48}
    ]
    assert rows["tasks"] == []


def test_efficiency_rows_count_build_calls_and_the_last_ts_per_task_id_across_runs_and_time(sqlite_dir):
    conn = sqlite3.connect(sqlite_dir / "cox.db")
    conn.execute(SEED_DDL[3])
    for call in (
        _call("c1", "x-1", 1, "build", "mid", "sonnet", 0.5, 2, 10, "2026-09-10T04:00:00+00:00"),
        _call("c2", "x-2", 1, "build", "mid", "sonnet", 0.25, 1, 10, "2026-09-25T04:00:00+00:00"),
        _call("c3", "x-2", 2, "review_charter", "mid", "sonnet", 0.25, 1, 10, "2026-09-25T05:00:00+00:00"),
    ):
        conn.execute(
            f"INSERT INTO node_calls ({', '.join(call)}, task_id) VALUES ({', '.join('?' * (len(call) + 1))})", (*call.values(), "t1")
        )
    conn.commit()
    conn.close()
    rows = run_store.efficiency_rows(sqlite_dir, "2026-09-24")
    assert [(c["day"], c["cost_usd"], c["turns"]) for c in rows["calls"]] == [("2026-09-25", 0.5, 2)]
    assert rows["tasks"] == [{"task_id": "t1", "builds": 2, "last_ts": "2026-09-25T05:00:00+00:00"}]


def test_efficiency_rows_of_no_store_or_a_store_without_the_table_is_empty(sqlite_dir):
    assert run_store.efficiency_rows(sqlite_dir, "2026-09-24") == {"calls": [], "tasks": []}
    conn = sqlite3.connect(sqlite_dir / "cox.db")
    conn.execute(SEED_DDL[0])
    conn.commit()
    conn.close()
    assert run_store.efficiency_rows(sqlite_dir, "2026-09-24") == {"calls": [], "tasks": []}


def test_a_store_missing_a_table_reads_as_empty(sqlite_dir):
    conn = sqlite3.connect(sqlite_dir / "cox.db")
    conn.execute(SEED_DDL[0])
    conn.commit()
    conn.close()
    assert run_store.phase_names(sqlite_dir, "r1") == []
