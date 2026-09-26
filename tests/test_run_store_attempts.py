import sqlite3

from agent_tools import run_store

V4 = "run_id TEXT, task_id TEXT, seq INTEGER, phase_id TEXT, kind TEXT, reason TEXT, ts TEXT"
V5 = V4 + ", cause TEXT, cause_why TEXT"


def attempts_table(runs_dir, columns, *rows):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute(f"CREATE TABLE attempts ({columns})")
    for row in rows:
        conn.execute(f"INSERT INTO attempts ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    conn.close()


def test_attempt_causes_reads_the_cause_columns_at_schema_5_and_filters_by_ts(tmp_path):
    early = {"kind": "review", "reason": "old", "ts": "2026-09-01T00:00:00+00:00", "cause": "unknown", "cause_why": "w0"}
    late = {"kind": "build", "reason": "r", "ts": "2026-09-25T04:00:00+00:00", "cause": "timeout", "cause_why": "slow"}
    attempts_table(tmp_path, V5, early, late)
    assert run_store.attempt_causes(tmp_path, "2026-09-25") == [late]


def test_attempt_causes_for_reads_run_task_seq_and_cause_of_the_named_runs_only(tmp_path):
    mine = {"run_id": "i-1", "task_id": "t", "seq": 1, "kind": "build", "reason": "r", "ts": "2026-09-25", "cause": "harness"}
    attempts_table(tmp_path, V5, mine, {**mine, "run_id": "i-2", "cause": "timeout"})
    assert run_store.attempt_causes_for(tmp_path, ["i-1"]) == [{"run_id": "i-1", "task_id": "t", "seq": 1, "cause": "harness"}]


def test_attempt_causes_for_is_empty_with_no_store_and_reads_none_at_schema_4(tmp_path):
    assert run_store.attempt_causes_for(tmp_path, ["i-1"]) == []
    attempts_table(tmp_path, V4, {"run_id": "i-1", "task_id": "t", "seq": 1, "kind": "build", "reason": "r", "ts": "x"})
    assert run_store.attempt_causes_for(tmp_path, ["i-1"]) == [{"run_id": "i-1", "task_id": "t", "seq": 1, "cause": None}]
    assert run_store.attempt_causes_for(tmp_path, []) == []


def test_attempt_causes_reads_none_for_both_cause_fields_at_schema_4(tmp_path):
    attempts_table(tmp_path, V4, {"kind": "build", "reason": "r", "ts": "2026-09-25T04:00:00+00:00"})
    assert run_store.attempt_causes(tmp_path, "") == [
        {"kind": "build", "cause": None, "cause_why": None, "reason": "r", "ts": "2026-09-25T04:00:00+00:00"}
    ]


def test_attempt_causes_is_empty_with_no_store(tmp_path):
    assert run_store.attempt_causes(tmp_path, "") == []


def test_attempt_causes_is_empty_when_the_store_has_no_attempts_table(tmp_path):
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT)")
    conn.close()
    assert run_store.attempt_causes(tmp_path, "") == []


def test_attempts_columns_names_the_columns_of_a_sqlite_attempts_table(tmp_path):
    attempts_table(tmp_path, V4 + ", cause TEXT")
    conn = run_store.connect_readonly(tmp_path)
    assert {"kind", "cause"} <= run_store._attempts_columns(conn, "?")
    assert "cause_why" not in run_store._attempts_columns(conn, "?")
    conn.close()
