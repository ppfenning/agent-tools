import sqlite3

from agent_tools.stats_schema import (
    CALLS_COLUMNS,
    FAILURE_CLASSES,
    OUTCOMES,
    RUNS_COLUMNS,
    TASKS_COLUMNS,
    Column,
    column_ddl,
    connect,
    ensure_schema,
    table_sql,
)


def test_column_ddl_renders_a_not_null_integer_default():
    col = Column("challenger", "INTEGER", default=0, not_null=True)
    assert column_ddl(col) == "challenger INTEGER NOT NULL DEFAULT 0"


def test_column_ddl_renders_a_string_default():
    col = Column("vendor", "TEXT", default="claude-code")
    assert column_ddl(col) == "vendor TEXT DEFAULT 'claude-code'"


def test_runs_columns_match_spec_names_verbatim():
    assert [c.name for c in RUNS_COLUMNS] == [
        "run_id",
        "started_at",
        "ended_at",
        "cartridge_sha",
        "cartridge_team",
        "provider_profile",
        "provider_profile_sha",
        "vendor",
        "host",
        "launched_by",
        "principal",
        "human_minutes",
        "schema_version",
    ]


def test_calls_columns_match_spec_names_verbatim_and_keep_the_join_columns():
    assert [c.name for c in CALLS_COLUMNS] == [
        "run_id",
        "seq",
        "role",
        "attempt",
        "tier",
        "model",
        "model_id",
        "cost_usd",
        "turns",
        "duration_ms",
        "input_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "output_tokens",
        "tools",
        "trace_path",
        "failure_class",
        "challenger",
        "task_id",
        "join_confidence",
        "recovered_from_trace",
    ]


def test_challenger_column_has_a_zero_default_and_is_not_null():
    challenger = next(c for c in CALLS_COLUMNS if c.name == "challenger")
    assert challenger.default == 0
    assert challenger.not_null is True


def test_recovered_from_trace_column_has_a_zero_default_and_is_not_null():
    """Named recovered_from_trace, not e.g. 'source', so a query reads its meaning
    without a lookup: 1 means this row's cost_usd/turns/failure_class came from a
    trace's terminal result line (spec §1 fact 5) because the run wrote no usage
    record, not from a usage record's calls[] entry. Zero default so every row a
    usage record produces reads back distinguishable without the ingester setting it."""
    recovered = next(c for c in CALLS_COLUMNS if c.name == "recovered_from_trace")
    assert recovered.default == 0
    assert recovered.not_null is True


def test_tasks_columns_match_spec_names_verbatim():
    assert [c.name for c in TASKS_COLUMNS] == [
        "run_id",
        "task_id",
        "ticket",
        "phase",
        "initiative",
        "attempt",
        "outcome",
        "outcome_source",
        "review_rounds",
        "arbitration_verdict",
        "fix_loop_rounds",
        "cost_usd",
        "reason",
        "outcome_kind",
        "handoff_verdict",
        "charter_verdict",
        "adversary_verdict",
        "arbiter_verdict",
        "arbiter_sided_with",
        "arbiter_state",
        "fix_loop_attempts",
        "fix_loop_stopped",
        "plan_gate_verdict",
    ]


def test_the_gate_fact_columns_are_nullable_with_no_default():
    assert all(c.default is None and not c.not_null for c in TASKS_COLUMNS[-9:])


def test_outcomes_enum_is_the_closed_set():
    assert OUTCOMES == ("landed", "quarantined", "budget_stop", "skipped", "unknown")
    assert len(set(OUTCOMES)) == len(OUTCOMES)


def test_failure_classes_enum_is_the_closed_set():
    assert FAILURE_CLASSES == (
        "budget_stop",
        "tool_error",
        "empty_patch",
        "refused",
        "ok",
        "unknown",
    )
    assert len(set(FAILURE_CLASSES)) == len(FAILURE_CLASSES)


def test_table_sql_renders_the_runs_table_with_a_primary_key():
    sql = table_sql("runs", RUNS_COLUMNS, "run_id")
    assert sql.startswith("CREATE TABLE IF NOT EXISTS runs (")
    assert "run_id TEXT" in sql
    assert "PRIMARY KEY (run_id)" in sql


def test_connect_creates_all_three_tables(tmp_path):
    conn = connect(tmp_path / "stats.db")
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"runs", "calls", "tasks"}.issubset(names)


def test_ensure_schema_adds_a_missing_column_with_its_default_without_dropping_rows(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "stats.db"))
    conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT)")
    conn.execute("INSERT INTO runs (run_id, started_at) VALUES ('r1', 't0')")
    conn.commit()

    ensure_schema(conn)

    row = conn.execute("SELECT run_id, vendor FROM runs WHERE run_id = 'r1'").fetchone()
    assert row == ("r1", "claude-code")


def test_ensure_schema_adds_model_id_to_an_existing_calls_table_and_keeps_its_rows(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "stats.db"))
    conn.execute("CREATE TABLE calls (run_id TEXT, seq INTEGER, model TEXT)")
    conn.execute("INSERT INTO calls (run_id, seq, model) VALUES ('r1', 0, 'haiku')")
    conn.commit()

    ensure_schema(conn)

    assert conn.execute("SELECT model, model_id FROM calls").fetchall() == [("haiku", None)]


def test_a_calls_row_inserted_without_challenger_reads_back_zero_not_null(tmp_path):
    conn = connect(tmp_path / "stats.db")
    conn.execute("INSERT INTO calls (run_id, seq, role) VALUES ('r1', 1, 'build')")
    conn.commit()

    value = conn.execute("SELECT challenger FROM calls WHERE run_id = 'r1'").fetchone()[0]

    assert value == 0
    assert value is not None
