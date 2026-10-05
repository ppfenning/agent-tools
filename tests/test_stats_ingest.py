import json
import os
import sqlite3
import subprocess
import sys
import types

import pytest

from agent_tools import stats_ingest
from agent_tools.events import Event
from agent_tools.stats_schema import connect


def _write_run(runs_dir, run_id, *, tasks=(), log_lines=(), usage=None, node=None, launched=None):
    if usage is not None:
        (runs_dir / f"{run_id}.usage.json").write_text(json.dumps(usage))
    if launched is not None:
        (runs_dir / f"{run_id}.launched.json").write_text(json.dumps(launched))
    if node is not None:
        (runs_dir / f"{run_id}:node.json").write_text(json.dumps(node))
    for phase, ticket, record in tasks:
        d = runs_dir / run_id / "tasks" / phase
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ticket}.json").write_text(json.dumps(record))
    if log_lines:
        (runs_dir / f"{run_id}.log").write_text("\n".join(log_lines) + "\n")


def _write_trace(runs_dir, run_id, name, events):
    d = runs_dir / f"{run_id}-trace"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def test_run_row_reads_the_first_nodes_identity_and_sums_human_minutes():
    row = stats_ingest.run_row(
        "r1", {"summary": {}},
        [{"cartridge_sha": "abc", "cartridge_team": "core", "principal": "pat", "human_minutes": 3},
         {"human_minutes": 2}],
        {"launched_by": "session-x"},
    )
    assert row["cartridge_sha"] == "abc"
    assert row["cartridge_team"] == "core"
    assert row["launched_by"] == "session-x"
    assert row["human_minutes"] == 5
    assert row["vendor"] == "claude-code"


def test_call_rows_numbers_attempts_per_role_in_call_order():
    usage = {"calls": [{"role": "build"}, {"role": "build"}, {"role": "review"}]}
    rows = stats_ingest.call_rows("r1", usage)
    assert [r["attempt"] for r in rows] == [1, 2, 1]
    assert [r["seq"] for r in rows] == [0, 1, 2]
    assert all(r["task_id"] is None and r["join_confidence"] is None for r in rows)
    assert all(r["recovered_from_trace"] == 0 for r in rows)


def test_call_rows_reads_challenger_off_the_calls_own_reason_marker():
    usage = {"calls": [{"role": "review", "reason": "challenger"}, {"role": "review", "reason": "default"}]}
    rows = stats_ingest.call_rows("r1", usage)
    assert [r["challenger"] for r in rows] == [1, 0]


def test_call_rows_challenger_is_zero_never_null_when_no_reason_is_present():
    usage = {"calls": [{"role": "review"}]}
    [row] = stats_ingest.call_rows("r1", usage)
    assert row["challenger"] == 0


def test_ingest_writes_challenger_per_call_and_tallies_it_in_the_report(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1",
        usage={"calls": [
            {"role": "review", "reason": "challenger"},
            {"role": "review", "reason": "default"},
        ]},
    )
    db_path = tmp_path / "stats.db"

    report = stats_ingest.ingest(runs_dir, db_path)

    conn = connect(db_path)
    rows = conn.execute("SELECT challenger FROM calls WHERE run_id = 'run1' ORDER BY seq").fetchall()
    conn.close()
    assert [r[0] for r in rows] == [1, 0]
    assert report.challenger_calls == 1


def test_recovered_call_rows_reads_cost_turns_and_failure_class_off_each_traces_final_result_line():
    traces = [
        ("runs/r1-trace/decompose-1.jsonl", [
            {"type": "result", "total_cost_usd": 0.3936, "subtype": "error_max_budget_usd", "num_turns": 12, "is_error": True},
        ]),
    ]
    rows = stats_ingest.recovered_call_rows("r1", traces)
    assert rows[0]["role"] == "decompose"
    assert rows[0]["attempt"] == 1
    assert rows[0]["cost_usd"] == 0.3936
    assert rows[0]["turns"] == 12
    assert rows[0]["failure_class"] == "budget_stop"
    assert rows[0]["trace_path"] == "runs/r1-trace/decompose-1.jsonl"
    assert rows[0]["recovered_from_trace"] == 1
    assert rows[0]["task_id"] is None and rows[0]["join_confidence"] is None


def test_task_row_carries_the_records_own_composite_task_id():
    record = {"run_id": "r1:p1:t1", "ticket": "t1"}
    row = stats_ingest.task_row("r1", "p1", "t1", record, gate_diffs=(), log_events=())
    assert row["task_id"] == "r1:p1:t1"
    assert row["outcome"] == "unknown"


def test_task_row_falls_back_to_a_constructed_task_id_when_the_record_names_none():
    row = stats_ingest.task_row("r1", "p1", "t1", {}, gate_diffs=(), log_events=())
    assert row["task_id"] == "r1:p1:t1"


def test_task_row_ignores_a_budget_stop_event_but_still_honors_a_quarantine_event():
    """A budget_stop event is present in log_events (events.py did parse it) and still
    sets no outcome; a task_quarantined event for a DIFFERENT ticket in the same list
    is honored, so the abstention is a deliberate refusal to read that event kind,
    not an absence of one."""
    log_events = [Event("r1", "budget_stop", 0, {}), Event("r1", "task_quarantined", 1, {"task": "t2", "reason": "x"})]
    row_t1 = stats_ingest.task_row("r1", "p1", "t1", {"ticket": "t1"}, gate_diffs=(), log_events=log_events)
    row_t2 = stats_ingest.task_row("r1", "p1", "t2", {"ticket": "t2"}, gate_diffs=(), log_events=log_events)
    assert (row_t1["outcome"], row_t1["outcome_source"]) == ("unknown", "unknown")
    assert (row_t2["outcome"], row_t2["outcome_source"]) == ("quarantined", "log_line")


def test_task_row_reads_the_arbiter_skip_string_as_an_approve_verdict():
    record = {"ticket": "t1", "arbitration": "arbiter: skipped (both approved)"}
    row = stats_ingest.task_row("r1", "p1", "t1", record, gate_diffs=(), log_events=())
    assert row["arbitration_verdict"] == "approve"


def test_task_row_fills_outcome_kind_from_the_last_attempts_kind():
    record = {"ticket": "t1", "attempts": [{"kind": "refused"}, {"kind": "unverified"}]}
    row = stats_ingest.task_row("r1", "p1", "t1", record, gate_diffs=(), log_events=())
    assert row["outcome_kind"] == "unverified"


def test_task_row_falls_back_to_the_phase_records_own_kind_with_no_attempts():
    record = {"ticket": "t1", "kind": "infra"}
    row = stats_ingest.task_row("r1", "p1", "t1", record, gate_diffs=(), log_events=())
    assert row["outcome_kind"] == "infra"


def test_task_row_leaves_outcome_kind_null_with_neither_attempts_nor_kind():
    record = {"ticket": "t1", "reason": "no_work"}
    row = stats_ingest.task_row("r1", "p1", "t1", record, gate_diffs=(), log_events=())
    assert row["outcome_kind"] is None


def test_rollup_task_costs_sums_only_joined_calls_and_leaves_none_for_no_joined_call():
    calls = [
        {"task_id": "t1", "join_confidence": "heuristic", "cost_usd": 1.5},
        {"task_id": "t1", "join_confidence": "none", "cost_usd": 9.0},
    ]
    tasks = [{"task_id": "t1", "cost_usd": None}, {"task_id": "t2", "cost_usd": None}]
    rolled = {t["task_id"]: t for t in stats_ingest.rollup_task_costs(calls, tasks)}
    assert rolled["t1"]["cost_usd"] == 1.5
    assert rolled["t2"]["cost_usd"] is None


def test_ingest_rolls_up_only_the_joined_calls_cost_onto_the_tasks_row(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1",
        tasks=[("p1", "t1", {"ticket": "t1"})],
        usage={"calls": [{"role": "build", "cost_usd": 1.5}, {"role": "review", "cost_usd": 9.0}]},
    )
    db_path = tmp_path / "stats.db"
    stats_ingest.ingest(runs_dir, db_path)

    conn = connect(db_path)
    value = conn.execute("SELECT cost_usd FROM tasks WHERE task_id = 'run1:p1:t1'").fetchone()[0]
    conn.close()
    assert value == 1.5


def test_ingest_is_idempotent(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1",
        tasks=[("p1", "t1", {"landed": True})],
        usage={"calls": [{"role": "build", "cost_usd": 1.0}]},
        launched={"launched_by": "s1"},
    )
    db_path = tmp_path / "stats.db"

    first = stats_ingest.ingest(runs_dir, db_path)
    conn = connect(db_path)
    first_tasks = conn.execute("SELECT * FROM tasks").fetchall()
    first_calls = conn.execute("SELECT * FROM calls").fetchall()
    first_runs = conn.execute("SELECT * FROM runs").fetchall()
    conn.close()

    second = stats_ingest.ingest(runs_dir, db_path)
    conn = connect(db_path)
    second_tasks = conn.execute("SELECT * FROM tasks").fetchall()
    second_calls = conn.execute("SELECT * FROM calls").fetchall()
    second_runs = conn.execute("SELECT * FROM runs").fetchall()
    conn.close()

    assert first.runs_ingested == second.runs_ingested == 1
    assert first_tasks == second_tasks
    assert first_calls == second_calls
    assert first_runs == second_runs


def test_ingest_upserts_rather_than_duplicating_on_a_changed_record(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"landed": True})])
    db_path = tmp_path / "stats.db"
    stats_ingest.ingest(runs_dir, db_path)

    _write_run(
        runs_dir, "run1",
        tasks=[("p1", "t1", {"landed": False})],
        node={"gate_diffs": [{"target": "t1", "outcome": "quarantined"}]},
    )
    stats_ingest.ingest(runs_dir, db_path)

    conn = connect(db_path)
    rows = conn.execute("SELECT outcome FROM tasks WHERE run_id = 'run1'").fetchall()
    conn.close()
    assert rows == [("quarantined",)]


def test_a_tasks_row_with_no_derivable_attempt_reads_back_as_none_through_the_real_schema(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"ticket": "t1"})])
    db_path = tmp_path / "stats.db"
    stats_ingest.ingest(runs_dir, db_path)

    conn = connect(db_path)
    value = conn.execute("SELECT attempt FROM tasks WHERE task_id = 'run1:p1:t1'").fetchone()[0]
    conn.close()
    assert value is None


def test_ingest_raises_naming_the_absolute_path_when_the_runs_dir_is_missing(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError) as exc_info:
        stats_ingest.ingest(missing, tmp_path / "stats.db")
    assert str(missing.resolve()) in str(exc_info.value)


def test_ingest_raises_naming_the_absolute_path_when_the_runs_dir_holds_no_run_records(tmp_path):
    empty = tmp_path / "runs"
    empty.mkdir()
    with pytest.raises(FileNotFoundError) as exc_info:
        stats_ingest.ingest(empty, tmp_path / "stats.db")
    assert str(empty.resolve()) in str(exc_info.value)


def _write_work_item(work_store_root, initiative, phase, ticket, state):
    d = work_store_root / initiative / phase
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticket}.md").write_text(f"---\nstate: {state}\n---\n\nbody\n")


def test_ingest_reads_landed_from_the_work_store_when_the_run_record_carries_no_landed_field(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"ticket": "t1"})])
    work_store_root = tmp_path / "work"
    _write_work_item(work_store_root, "other-init", "other-phase", "t1", "done")
    db_path = tmp_path / "stats.db"

    stats_ingest.ingest(runs_dir, db_path, work_store_root=work_store_root)

    conn = connect(db_path)
    row = conn.execute("SELECT outcome, outcome_source FROM tasks WHERE task_id = 'run1:p1:t1'").fetchone()
    conn.close()
    assert row == ("landed", "work_store")


def test_ingest_reads_the_work_store_ahead_of_a_scoped_gate_diffs_entry(tmp_path):
    """A node record's gate_diffs can carry a 'skipped' outcome for this ticket
    (a diff taken before the ticket landed) while the work store already shows
    state: done. The work store must win, not be shadowed by that diff."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1",
        tasks=[("p1", "t1", {"ticket": "t1"})],
        node={"gate_diffs": [{"target": "t1", "outcome": "skipped"}]},
    )
    work_store_root = tmp_path / "work"
    _write_work_item(work_store_root, "other-init", "other-phase", "t1", "done")
    db_path = tmp_path / "stats.db"

    stats_ingest.ingest(runs_dir, db_path, work_store_root=work_store_root)

    conn = connect(db_path)
    row = conn.execute("SELECT outcome, outcome_source FROM tasks WHERE task_id = 'run1:p1:t1'").fetchone()
    conn.close()
    assert row == ("landed", "work_store")


def test_ingest_leaves_the_task_unknown_when_no_work_store_root_is_given(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"ticket": "t1", "initiative": "init1"})])
    db_path = tmp_path / "stats.db"

    stats_ingest.ingest(runs_dir, db_path)

    conn = connect(db_path)
    row = conn.execute("SELECT outcome, outcome_source FROM tasks WHERE task_id = 'run1:p1:t1'").fetchone()
    conn.close()
    assert row == ("unknown", "unknown")


def test_discover_runs_finds_a_run_that_only_has_a_node_record(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "run1:build.json").write_text("{}")
    assert stats_ingest.discover_runs(runs_dir) == ["run1"]


def test_discover_runs_finds_a_run_that_only_has_a_trace_directory(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_trace(runs_dir, "run1", "build-1.jsonl", [{"type": "result", "total_cost_usd": 0.1}])
    assert stats_ingest.discover_runs(runs_dir) == ["run1"]


def test_load_run_reads_trace_files_only_when_there_is_no_usage_record(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    path = _write_trace(runs_dir, "run1", "build-1.jsonl", [{"type": "result", "total_cost_usd": 0.1, "num_turns": 3}])

    loaded = stats_ingest.load_run(runs_dir, "run1")

    assert loaded["traces"] == [(str(path), [{"type": "result", "total_cost_usd": 0.1, "num_turns": 3}])]


def test_load_run_ignores_a_trace_directory_when_a_usage_record_is_present(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", usage={"calls": []})
    _write_trace(runs_dir, "run1", "build-1.jsonl", [{"type": "result", "total_cost_usd": 0.1}])

    loaded = stats_ingest.load_run(runs_dir, "run1")

    assert loaded["traces"] == []


def test_load_run_orders_trace_files_by_node_order_not_alphabetical_filename(tmp_path):
    """`scope_epic` precedes `build` in NODE_ORDER but follows it alphabetically;
    `_read_traces` must sort by the graph's own order the same way `_node_sort_key`
    already does for `<run>:<node>.json`, so a recovered call's `seq` reflects
    execution order rather than the accident of two role names' alphabet."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_trace(runs_dir, "run1", "build-1.jsonl", [{"type": "result", "total_cost_usd": 0.1}])
    _write_trace(runs_dir, "run1", "scope_epic-1.jsonl", [{"type": "result", "total_cost_usd": 0.2}])

    loaded = stats_ingest.load_run(runs_dir, "run1")

    ordered_paths = [path for path, _ in loaded["traces"]]
    assert ordered_paths == [
        str(runs_dir / "run1-trace" / "scope_epic-1.jsonl"),
        str(runs_dir / "run1-trace" / "build-1.jsonl"),
    ]


def test_load_run_leaves_calls_jsonl_empty_when_only_usage_json_is_present(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", usage={"calls": []})

    loaded = stats_ingest.load_run(runs_dir, "run1")

    assert loaded["calls_jsonl"] == []
    assert loaded["usage"] == {"calls": []}
    assert loaded["traces"] == []


def test_load_run_reads_calls_jsonl_and_tags_its_source_when_usage_is_absent(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    calls = [
        {"role": "build", "cost_usd": 0.1, "trace": "run1-trace/build-1.jsonl", "ts": "t1", "ok": True},
        {"role": "review", "cost_usd": 0.2, "trace": "run1-trace/review-1.jsonl", "ts": "t2", "ok": False, "error": "e"},
    ]
    (runs_dir / "run1.calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls) + "\n")
    _write_trace(runs_dir, "run1", "build-1.jsonl", [{"type": "result", "total_cost_usd": 0.1}])

    loaded = stats_ingest.load_run(runs_dir, "run1")

    assert loaded["calls_jsonl"] == [{**c, "source": "calls_jsonl"} for c in calls]
    assert loaded["traces"] == []


def test_load_run_falls_back_to_trace_recovery_when_neither_usage_json_nor_calls_jsonl_exist(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    path = _write_trace(runs_dir, "run1", "build-1.jsonl", [{"type": "result", "total_cost_usd": 0.1, "num_turns": 3}])

    loaded = stats_ingest.load_run(runs_dir, "run1")

    assert loaded["calls_jsonl"] == []
    assert loaded["traces"] == [(str(path), [{"type": "result", "total_cost_usd": 0.1, "num_turns": 3}])]


def test_ingest_assigns_seq_to_recovered_calls_in_node_order_not_alphabetical_order(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_trace(runs_dir, "run1", "build-1.jsonl", [{"type": "result", "total_cost_usd": 0.1}])
    _write_trace(runs_dir, "run1", "scope_epic-1.jsonl", [{"type": "result", "total_cost_usd": 0.2}])

    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    rows = conn.execute("SELECT seq, role FROM calls WHERE run_id = 'run1' ORDER BY seq").fetchall()
    conn.close()
    assert rows == [(0, "scope_epic"), (1, "build")]


_NODE_CALLS_COLUMNS = (
    "call_id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, phase_id TEXT, task_id TEXT, role TEXT, tier TEXT, "
    "model_alias TEXT, model_id TEXT, claude_code_version TEXT, cost_usd REAL, ceiling_usd REAL, "
    "ceiling_source TEXT, turns INTEGER, duration_ms INTEGER, input_tokens INTEGER, cache_read_tokens INTEGER, "
    "cache_creation_tokens INTEGER, input_total INTEGER, output_tokens INTEGER, ok BOOL, ts TEXT, "
    "requested_tier TEXT, chosen_tier TEXT, decision_reason TEXT, router_tier TEXT, router_reason TEXT, "
    "ticket_key TEXT, outcome_key TEXT, system_one_prediction TEXT, system_one_confidence REAL, "
    "decision_json TEXT, detail_json TEXT"
)
_STORE_TABLES = {
    "node_calls": _NODE_CALLS_COLUMNS,
    "runs": "run_id TEXT PRIMARY KEY, launched_at TEXT, ended_at TEXT, status TEXT",
    "phases": "run_id TEXT, phase_id TEXT, ts TEXT, record_json TEXT, PRIMARY KEY (run_id, phase_id)",
}
_ENDED = "2026-09-25T05:03:46+00:00"


def _write_store(runs_dir, run_id, *, roles=(), phases=(), models=()):
    """A `cox.db` holding one ended run: a `node_calls` row per role and a `phases` row per phase.
    `models` is an optional `(model_alias, model_id)` pair per role, in role order."""
    conn = sqlite3.connect(runs_dir / "cox.db")
    for name, columns in _STORE_TABLES.items():
        conn.execute(f"CREATE TABLE {name} ({columns})")
    conn.execute("INSERT INTO runs (run_id, ended_at, status) VALUES (?, ?, 'ok')", (run_id, _ENDED))
    for seq, role in enumerate(roles):
        alias, model_id = models[seq] if seq < len(models) else (None, None)
        conn.execute(
            "INSERT INTO node_calls (call_id, run_id, seq, role, model_alias, model_id, cost_usd, ok, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, 0.5, 1, ?)",
            (f"c{seq}", run_id, seq, role, alias, model_id, f"2026-09-25T04:3{seq}:00+00:00"),
        )
    for n, phase in enumerate(phases):
        record = {"manifest_record": {"run_id": f"{run_id}:{phase}", "cartridge_team": "pat", "human_minutes": 1.0}}
        conn.execute(
            "INSERT INTO phases (run_id, phase_id, ts, record_json) VALUES (?, ?, ?, ?)",
            (run_id, phase, f"2026-09-25T04:4{n}:00+00:00", json.dumps(record)),
        )
    conn.commit()
    conn.close()


def _write_store_trace(runs_dir, run_id, events_by_call, monkeypatch):
    """One plain-text day file, read through a stand-in decompressor so the test needs no zstandard."""
    stand_in = types.SimpleNamespace(
        ZstdDecompressor=lambda: types.SimpleNamespace(stream_reader=lambda fh, read_across_frames: fh)
    )
    monkeypatch.setitem(sys.modules, "zstandard", stand_in)
    path = runs_dir / "traces" / "2026" / "09" / "25" / f"{run_id}.jsonl.zst"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"run_id": run_id, "call_id": call_id, "seq": seq, "event": event}
        for call_id, events in events_by_call.items()
        for seq, event in enumerate(events)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_a_store_only_ended_run_is_discovered_and_ingested_with_its_calls_and_phases(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_store(runs_dir, "sr1", roles=("scope_epic", "build"), phases=("build", "plan"))

    assert stats_ingest.discover_runs(runs_dir) == ["sr1"]
    loaded = stats_ingest.load_run(runs_dir, "sr1")
    assert [m["run_id"] for m in loaded["node_records"]] == ["sr1:plan", "sr1:build"]
    assert (loaded["calls_jsonl"], loaded["traces"]) == ([], [])

    report = stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    calls = conn.execute("SELECT role FROM calls WHERE run_id = 'sr1' ORDER BY seq").fetchall()
    team = conn.execute("SELECT cartridge_team FROM runs WHERE run_id = 'sr1'").fetchone()
    conn.close()
    assert report.runs_ingested == 1
    assert calls == [("scope_epic",), ("build",)]
    assert team == ("pat",)


def test_a_node_call_with_a_model_id_lands_with_that_id_and_its_alias_still_in_model(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_store(runs_dir, "sr1", roles=("build",), models=(("haiku", "claude-haiku-4-5-20251001"),))

    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    row = conn.execute("SELECT model, model_id FROM calls WHERE run_id = 'sr1'").fetchone()
    conn.close()
    assert row == ("haiku", "claude-haiku-4-5-20251001")


def test_a_node_call_with_no_model_id_lands_with_model_id_null_not_the_alias(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_store(runs_dir, "sr1", roles=("build",), models=(("haiku", None),))

    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    row = conn.execute("SELECT model, model_id FROM calls WHERE run_id = 'sr1'").fetchone()
    conn.close()
    assert row == ("haiku", None)


def test_a_run_with_files_ignores_the_store_rows_for_the_same_run(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "r1", usage={"calls": [{"role": "build", "cost_usd": 1.0}]}, node={"run_id": "r1:node"})
    _write_store(runs_dir, "r1", roles=("plan", "review"), phases=("plan",))

    loaded = stats_ingest.load_run(runs_dir, "r1")

    assert loaded["usage"] == {"calls": [{"role": "build", "cost_usd": 1.0}]}
    assert loaded["node_records"] == [{"run_id": "r1:node"}]
    assert loaded["unparsed"] == []


def test_a_call_trace_is_read_from_the_store_when_its_loose_file_is_gone(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    gone = str(runs_dir / "r1-trace" / "build-1.jsonl")
    _write_run(runs_dir, "r1", usage={"calls": [{"id": "c1", "trace": gone}, {"id": "c2"}]})
    _write_store_trace(runs_dir, "r1", {"c1": [{"n": 0}, {"n": 1}], "c2": [{"n": 2}]}, monkeypatch)

    loaded = stats_ingest.load_run(runs_dir, "r1")

    assert loaded["call_traces"] == {gone: [{"n": 0}, {"n": 1}], "c2": [{"n": 2}]}
    assert loaded["unparsed"] == []


def test_traces_unavailable_skips_the_calls_and_records_the_store_once_instead_of_raising(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    (runs_dir / "traces").mkdir(parents=True)
    gone = str(runs_dir / "gone.jsonl")
    _write_run(runs_dir, "r1", usage={"calls": [{"id": "c1", "trace": gone}, {"id": "c2"}, {"id": "c3"}]})
    asked = []

    def unavailable(runs_dir, run_id, call):
        asked.append(call["id"])
        raise stats_ingest.run_store.TracesUnavailable("reading traces needs zstandard")

    monkeypatch.setattr(stats_ingest.run_store, "call_events", unavailable)

    loaded = stats_ingest.load_run(runs_dir, "r1")

    assert loaded["call_traces"] == {}
    assert loaded["unparsed"] == [gone, f"{runs_dir / 'traces'}: TracesUnavailable"]
    assert asked == ["c1"]


def test_a_missing_trace_with_nothing_in_the_store_stays_unclassified_and_unparsed(tmp_path):
    runs_dir = tmp_path / "runs"
    (runs_dir / "traces").mkdir(parents=True)
    gone = str(runs_dir / "r1-trace" / "build-1.jsonl")
    _write_run(runs_dir, "r1", usage={"calls": [{"id": "c1", "role": "build", "trace": gone}]})

    loaded = stats_ingest.load_run(runs_dir, "r1")
    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    failure_class = conn.execute("SELECT failure_class FROM calls WHERE run_id = 'r1'").fetchone()
    conn.close()
    assert (loaded["call_traces"], loaded["unparsed"]) == ({}, [gone])
    assert failure_class == (None,)


def test_a_corrupt_store_day_file_is_recorded_and_does_not_abort_the_ingest(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    gone = str(runs_dir / "gone.jsonl")
    _write_run(runs_dir, "r1", usage={"calls": [{"id": "c1", "trace": gone}]})
    _write_store_trace(runs_dir, "r1", {"c1": [{"n": 0}]}, monkeypatch)
    (runs_dir / "traces" / "2026" / "09" / "25" / "r1.jsonl.zst").write_text('{"call_id": "c1", "event": {}}\n')

    loaded = stats_ingest.load_run(runs_dir, "r1")

    assert (loaded["call_traces"], loaded["unparsed"]) == ({}, [gone, f"{runs_dir / 'traces'}: KeyError"])


def test_a_truncated_zstd_day_file_is_recorded_and_does_not_abort_the_ingest(tmp_path):
    pytest.importorskip("zstandard")
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    gone = str(runs_dir / "gone.jsonl")
    _write_run(runs_dir, "r1", usage={"calls": [{"id": "c1", "trace": gone}, {"id": "c2"}]})
    day = runs_dir / "traces" / "2026" / "09" / "25"
    day.mkdir(parents=True)
    (day / "r1.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd truncated")

    loaded = stats_ingest.load_run(runs_dir, "r1")

    assert (loaded["call_traces"], loaded["unparsed"]) == ({}, [gone, f"{runs_dir / 'traces'}: ZstdError"])


def test_a_store_only_runs_calls_get_their_failure_class_from_the_store_traces_by_id(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_store(runs_dir, "sr1", roles=("scope_epic", "build", "review"), phases=("plan",))
    ok = {"type": "result", "is_error": False, "subtype": "success"}
    stopped = {"type": "result", "is_error": True, "subtype": "error_max_budget_usd"}
    _write_store_trace(runs_dir, "sr1", {"c0": [ok], "c1": [stopped]}, monkeypatch)

    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    rows = conn.execute("SELECT role, failure_class, trace_path FROM calls WHERE run_id = 'sr1' ORDER BY seq").fetchall()
    conn.close()
    assert rows == [("scope_epic", "ok", None), ("build", "budget_stop", None), ("review", None, None)]


def test_fill_failure_classes_matches_a_call_by_the_key_it_is_given_instead_of_its_trace_path():
    calls = [{"role": "review", "trace_path": None, "failure_class": None}, {"role": "review", "trace_path": None, "failure_class": None}]
    traces = {"c0": [{"type": "result", "is_error": False}]}

    rows = stats_ingest.fill_failure_classes(calls, traces, "", ["c0", "c1"])

    assert [r["failure_class"] for r in rows] == ["ok", None]


def test_ingest_reads_the_stores_manifests_once_not_once_per_run(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_store(runs_dir, "sr1", roles=("build",), phases=("plan",))

    def rescans(runs_dir, run_id):
        raise AssertionError("phase_manifests re-scans the whole store on every call")

    monkeypatch.setattr(stats_ingest.run_store, "phase_manifests", rescans)

    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    team = conn.execute("SELECT cartridge_team FROM runs WHERE run_id = 'sr1'").fetchone()
    conn.close()
    assert team == ("pat",)


def test_ingest_recovers_a_budget_stopped_runs_cost_from_its_trace_result_line(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_trace(
        runs_dir, "run1", "build-1.jsonl",
        [{"type": "result", "total_cost_usd": 0.3936, "subtype": "error_max_budget_usd", "num_turns": 12, "is_error": True}],
    )

    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    row = conn.execute(
        "SELECT cost_usd, failure_class, recovered_from_trace FROM calls WHERE run_id = 'run1'"
    ).fetchone()
    conn.close()
    assert row == (0.3936, "budget_stop", 1)


def test_ingest_is_idempotent_for_a_trace_recovered_run(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_trace(
        runs_dir, "run1", "build-1.jsonl",
        [{"type": "result", "total_cost_usd": 0.3936, "subtype": "error_max_budget_usd", "num_turns": 12, "is_error": True}],
    )
    db_path = tmp_path / "stats.db"

    stats_ingest.ingest(runs_dir, db_path)
    conn = connect(db_path)
    first = conn.execute("SELECT * FROM calls WHERE run_id = 'run1'").fetchall()
    conn.close()

    stats_ingest.ingest(runs_dir, db_path)
    conn = connect(db_path)
    second = conn.execute("SELECT * FROM calls WHERE run_id = 'run1'").fetchall()
    conn.close()

    assert first == second


def test_ingest_recovers_the_tickets_own_measured_decompose_failure_cost(tmp_path):
    """2026-09-07: a decompose run died on error_max_budget_usd having spent $0.4454
    with no usage.json — the first of the two runs the ticket measured as $0.84
    invisible to every usage-based aggregate."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_trace(
        runs_dir, "run1", "decompose-1.jsonl",
        [{"type": "result", "total_cost_usd": 0.4454, "subtype": "error_max_budget_usd", "num_turns": 8, "is_error": True}],
    )

    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    cost = conn.execute("SELECT cost_usd FROM calls WHERE run_id = 'run1'").fetchone()[0]
    conn.close()
    assert cost == 0.4454


def test_ingest_does_not_report_a_runs_dir_empty_when_it_holds_only_node_records(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "run1:build.json").write_text(json.dumps({"cartridge_sha": "abc"}))

    report = stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    assert report.runs_ingested == 1
    conn = connect(tmp_path / "stats.db")
    row = conn.execute("SELECT cartridge_sha FROM runs WHERE run_id = 'run1'").fetchone()
    conn.close()
    assert row == ("abc",)


def test_ingest_reports_a_file_that_fails_to_parse_without_raising(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"ticket": "t1"})])
    bad = runs_dir / "run1:node.json"
    bad.write_text("{not json")

    report = stats_ingest.ingest(runs_dir, tmp_path / "stats.db")
    assert report.unparsed_count == 1
    assert str(bad) in report.unparsed_sample[0]


def test_ingest_reports_a_non_utf8_record_instead_of_raising(tmp_path):
    """A torn or binary-corrupted write is a parse failure, not a crash: runs_detail_screen.py's
    own reader treats OSError the same way, per its 'missing, torn or unreadable reads as {}'."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"ticket": "t1"})])
    bad = runs_dir / "run1:node.json"
    bad.write_bytes(b"\xff\xfe\x00not utf-8")

    report = stats_ingest.ingest(runs_dir, tmp_path / "stats.db")
    assert report.unparsed_count == 1
    assert str(bad) in report.unparsed_sample[0]


def test_ingest_reports_a_non_utf8_log_instead_of_raising(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"ticket": "t1"})])
    bad = runs_dir / "run1.log"
    bad.write_bytes(b"\xff\xfe\x00not utf-8")

    report = stats_ingest.ingest(runs_dir, tmp_path / "stats.db")
    assert report.unparsed_count == 1
    assert str(bad) in report.unparsed_sample[0]


def test_a_budget_stop_log_line_never_sets_either_tickets_outcome(tmp_path):
    """Run 17 was refused for a fixture with no budget-stop signal in it; this one carries one."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1",
        tasks=[("p1", "t1", {"ticket": "t1"}), ("p1", "t2", {"ticket": "t2"})],
        log_lines=["boom: error_max_budget_usd"],
    )
    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    rows = conn.execute("SELECT ticket, outcome, outcome_source FROM tasks WHERE run_id = 'run1' ORDER BY ticket").fetchall()
    conn.close()
    assert rows == [("t1", "unknown", "unknown"), ("t2", "unknown", "unknown")]


def test_gate_diffs_are_ordered_by_the_graphs_node_sequence_not_the_node_files_alphabetical_name(tmp_path):
    """`arbitrate` sorts alphabetically before `review_charter` but runs after it in the
    real graph (agent_tools/runs_detail.py NODE_ORDER); resolve_outcome takes the LAST
    scoped gate_diffs entry, so a node-record read order that trusted the filename
    alphabet would let review_charter's stale 'landed' overwrite arbitrate's real
    'quarantined' verdict."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"ticket": "t1"})])
    (runs_dir / "run1:review_charter.json").write_text(json.dumps({"gate_diffs": [{"target": "t1", "outcome": "landed"}]}))
    (runs_dir / "run1:arbitrate.json").write_text(json.dumps({"gate_diffs": [{"target": "t1", "outcome": "quarantined"}]}))

    stats_ingest.ingest(runs_dir, tmp_path / "stats.db")

    conn = connect(tmp_path / "stats.db")
    outcome = conn.execute("SELECT outcome FROM tasks WHERE task_id = 'run1:p1:t1'").fetchone()[0]
    conn.close()
    assert outcome == "quarantined"


def test_stats_ingest_help_exits_zero():
    from agent_tools.cli import build_parser

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["stats", "ingest", "--help"])
    assert exc_info.value.code == 0


def test_cli_stats_ingest_exits_nonzero_and_names_the_path_on_an_empty_runs_dir(tmp_path, capsys):
    from agent_tools.cli import main

    empty = tmp_path / "runs"
    empty.mkdir()
    code = main(["stats", "ingest", str(empty), "--db", str(tmp_path / "stats.db")])
    out = capsys.readouterr().out
    assert code == 1
    assert str(empty.resolve()) in out


def test_cli_stats_ingest_exits_zero_on_a_clean_run(tmp_path):
    from agent_tools.cli import main

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"landed": True})])
    code = main(["stats", "ingest", str(runs_dir), "--db", str(tmp_path / "stats.db")])
    assert code == 0


def test_cli_stats_ingest_reports_provider_profile_counts_by_source(tmp_path, capsys):
    from agent_tools.cli import main

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1", tasks=[("p1", "t1", {"landed": True})],
        node={"cartridge_sha": "abc", "provider_profile": "claude-code"},
    )
    code = main(["stats", "ingest", str(runs_dir), "--db", str(tmp_path / "stats.db")])
    out = capsys.readouterr().out
    assert code == 0
    assert "provider_profile: 0 from ledger, 1 from node record, 0 unresolved" in out


def test_run_join_holds_when_attempts_sum_to_the_build_call_count():
    assert stats_ingest.run_join_holds([2, 1], 3) is True


def test_run_join_holds_is_false_when_attempts_undercount_the_build_calls():
    assert stats_ingest.run_join_holds([2, 1], 4) is False


def test_assign_task_ids_pairs_build_calls_to_tickets_in_ticket_order_times_attempts():
    calls = [{"role": "build"}, {"role": "build"}, {"role": "build"}, {"role": "review"}]
    rows = stats_ingest.assign_task_ids(calls, ["t1", "t2"], [2, 1])
    assert [r["task_id"] for r in rows] == ["t1", "t1", "t2", None]
    assert [r["join_confidence"] for r in rows] == ["heuristic", "heuristic", "heuristic", "none"]


def test_assign_task_ids_leaves_every_call_unjoined_when_the_identity_fails():
    calls = [{"role": "build"}, {"role": "build"}]
    rows = stats_ingest.assign_task_ids(calls, ["t1"], [5])
    assert [r["task_id"] for r in rows] == [None, None]
    assert [r["join_confidence"] for r in rows] == ["none", "none"]


def test_fill_failure_classes_reads_ok_off_a_successful_calls_trace():
    calls = [{"trace_path": "run1-trace/build-1.jsonl", "failure_class": None}]
    traces = {
        "run1-trace/build-1.jsonl": [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
            {"type": "result", "is_error": False},
        ]
    }
    rows = stats_ingest.fill_failure_classes(calls, traces)
    assert rows[0]["failure_class"] == "ok"


def test_fill_failure_classes_reads_the_enum_value_off_a_failed_calls_trace():
    calls = [{"trace_path": "run1-trace/build-1.jsonl", "failure_class": None}]
    traces = {"run1-trace/build-1.jsonl": [{"type": "result", "is_error": True, "subtype": "error_max_budget_usd"}]}
    rows = stats_ingest.fill_failure_classes(calls, traces)
    assert rows[0]["failure_class"] == "budget_stop"


def test_fill_failure_classes_leaves_a_call_with_no_matching_trace_unset():
    calls = [{"trace_path": None, "failure_class": None}]
    rows = stats_ingest.fill_failure_classes(calls, {})
    assert rows[0]["failure_class"] is None


def _write_ledger(tmp_path, rows):
    path = tmp_path / "ledger.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_provider_profile_for_reads_the_ledgers_run_colon_node_keyed_row():
    rows = [{"key": "run1:build", "provider_profile": "vendor-a"}]
    assert stats_ingest.provider_profile_for("run1", rows) == "vendor-a"


def test_provider_profile_for_is_none_when_no_ledger_row_names_the_run():
    assert stats_ingest.provider_profile_for("run1", [{"key": "run2:build", "provider_profile": "vendor-a"}]) is None


def test_ingest_applies_the_join_only_where_the_counting_identity_holds(tmp_path):
    """Corpus-level coverage assertion: the fraction of calls carrying a non-NULL
    task_id must equal exactly what the identity predicts, not merely be > 0."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1",
        tasks=[("p1", "t1", {"ticket": "t1", "fix_loop": {"attempts": 2}}), ("p1", "t2", {"ticket": "t2"})],
        usage={"calls": [{"role": "build"}, {"role": "build"}, {"role": "build"}, {"role": "review"}]},
    )
    _write_run(
        runs_dir, "run2",
        tasks=[("p1", "t3", {"ticket": "t3", "fix_loop": {"attempts": 5}})],
        usage={"calls": [{"role": "build"}, {"role": "build"}, {"role": "review"}]},
    )
    db_path = tmp_path / "stats.db"

    stats_ingest.ingest(runs_dir, db_path)

    conn = connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    joined = conn.execute("SELECT COUNT(*) FROM calls WHERE task_id IS NOT NULL").fetchone()[0]
    run1_task_ids = conn.execute(
        "SELECT task_id FROM calls WHERE run_id = 'run1' AND role = 'build' ORDER BY seq"
    ).fetchall()
    run2_confidence = conn.execute("SELECT DISTINCT join_confidence FROM calls WHERE run_id = 'run2'").fetchall()
    conn.close()

    assert (total, joined) == (7, 3)
    assert run1_task_ids == [("run1:p1:t1",), ("run1:p1:t1",), ("run1:p1:t2",)]
    assert run2_confidence == [("none",)]


def test_ingest_fills_failure_class_from_each_calls_own_trace_and_never_leaves_a_resulted_call_null(tmp_path):
    """Two failing calls in one run: a review call whose own trace shows a tool
    error (no budget signal in it at all) and a build call whose own trace shows
    a real budget stop, plus a run-level log line naming budget_stop that
    belongs to the build call, not the review one. A failure_class reader that
    shared the whole run's log across both calls, instead of scoping to each
    call's own trace, would bleed that log line onto the review call too and
    misclassify it budget_stop; it must read tool_error there instead."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1",
        usage={"calls": [
            {"role": "review", "trace": "run1-trace/review-1.jsonl"},
            {"role": "build", "trace": "run1-trace/build-1.jsonl"},
        ]},
        log_lines=["boom: error_max_budget_usd"],
    )
    _write_trace(
        runs_dir, "run1", "review-1.jsonl",
        [
            {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True}]}},
            {"type": "result", "is_error": True},
        ],
    )
    _write_trace(
        runs_dir, "run1", "build-1.jsonl",
        [{"type": "result", "is_error": True, "subtype": "error_max_budget_usd"}],
    )
    db_path = tmp_path / "stats.db"

    stats_ingest.ingest(runs_dir, db_path)

    conn = connect(db_path)
    rows = conn.execute("SELECT role, failure_class FROM calls WHERE run_id = 'run1' ORDER BY seq").fetchall()
    conn.close()
    assert rows == [("review", "tool_error"), ("build", "budget_stop")]


def test_ingest_fills_host_and_provider_profile_from_the_ledger_for_every_run(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"landed": True})])
    _write_run(runs_dir, "run2", tasks=[("p1", "t2", {"landed": True})])
    ledger = _write_ledger(
        tmp_path,
        [
            {"key": "run1:build", "provider_profile": "vendor-a"},
            {"key": "run2:build", "provider_profile": "vendor-b"},
        ],
    )
    db_path = tmp_path / "stats.db"

    stats_ingest.ingest(runs_dir, db_path, ledger_path=ledger)

    conn = connect(db_path)
    rows = conn.execute("SELECT run_id, host, provider_profile FROM runs ORDER BY run_id").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["run1", "run2"]
    assert all(r[1] is not None for r in rows)
    assert [r[2] for r in rows] == ["vendor-a", "vendor-b"]


def test_provider_profile_from_nodes_reads_the_node_records_own_key():
    nodes = [{"cartridge_sha": "abc", "provider_profile": "claude-code"}]
    assert stats_ingest.provider_profile_from_nodes(nodes) == "claude-code"


def test_provider_profile_from_nodes_is_none_when_no_node_record_names_it():
    assert stats_ingest.provider_profile_from_nodes([{"cartridge_sha": "abc"}]) is None


def test_provider_profile_source_falls_back_to_the_node_record_when_the_ledger_is_silent():
    nodes = [{"provider_profile": "claude-code"}]
    profile, source = stats_ingest.provider_profile_source("run1", [], nodes)
    assert (profile, source) == ("claude-code", "node")


def test_provider_profile_source_prefers_the_ledger_over_the_node_record():
    rows = [{"key": "run1:build", "provider_profile": "vendor-a"}]
    nodes = [{"provider_profile": "vendor-b"}]
    profile, source = stats_ingest.provider_profile_source("run1", rows, nodes)
    assert (profile, source) == ("vendor-a", "ledger")


def test_provider_profile_source_is_none_and_unresolved_when_neither_source_names_the_run():
    assert stats_ingest.provider_profile_source("run1", [], []) == (None, "none")


def test_ingest_fills_provider_profile_from_the_node_record_when_the_ledger_has_no_row_for_this_run(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1", tasks=[("p1", "t1", {"landed": True})],
        node={"cartridge_sha": "abc", "provider_profile": "claude-code"},
    )
    db_path = tmp_path / "stats.db"

    stats_ingest.ingest(runs_dir, db_path, ledger_path=tmp_path / "no-such-ledger.jsonl")

    conn = connect(db_path)
    row = conn.execute("SELECT provider_profile FROM runs WHERE run_id = 'run1'").fetchone()
    conn.close()
    assert row == ("claude-code",)


def test_ingest_report_provider_profile_counts_sum_to_runs_ingested(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {"landed": True})])
    _write_run(
        runs_dir, "run2", tasks=[("p1", "t2", {"landed": True})],
        node={"cartridge_sha": "abc", "provider_profile": "claude-code"},
    )
    _write_run(runs_dir, "run3", tasks=[("p1", "t3", {"landed": True})])
    ledger = _write_ledger(tmp_path, [{"key": "run1:build", "provider_profile": "vendor-a"}])
    db_path = tmp_path / "stats.db"

    report = stats_ingest.ingest(runs_dir, db_path, ledger_path=ledger)

    assert report.provider_profile_from_ledger == 1
    assert report.provider_profile_from_node == 1
    assert report.provider_profile_unresolved == 1
    assert (
        report.provider_profile_from_ledger
        + report.provider_profile_from_node
        + report.provider_profile_unresolved
        == report.runs_ingested
    )


def _make_cartridges_repo(tmp_path, profile, content, commit_date):
    """A throwaway git checkout with one commit, at `commit_date`, adding
    `providers/<profile>.yaml` with `content`. Returns the repo path and that
    file's git blob sha, computed independently via `git hash-object` so the
    expected value in a test never comes from the same `log`/`rev-parse` pair
    `resolve_provider_profile_sha` itself runs."""
    repo = tmp_path / "coxswain-cartridges"
    (repo / "providers").mkdir(parents=True)
    path = repo / "providers" / f"{profile}.yaml"
    path.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "hash-object", str(path)], check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    env = {**os.environ, "GIT_AUTHOR_DATE": commit_date, "GIT_COMMITTER_DATE": commit_date}
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
            "commit", "-q", "-m", "profile",
        ],
        check=True, env=env,
    )
    return repo, sha


def test_resolve_provider_profile_sha_reads_the_blob_at_the_last_commit_before_started_at(tmp_path):
    repo, sha = _make_cartridges_repo(tmp_path, "claude-code", "budget_usd: 5\n", "2026-01-01T00:00:00")
    resolved = stats_ingest.resolve_provider_profile_sha(repo, "claude-code", "2026-01-02T00:00:00")
    assert resolved == sha


def test_resolve_provider_profile_sha_is_none_when_started_at_predates_every_commit(tmp_path):
    repo, _ = _make_cartridges_repo(tmp_path, "claude-code", "budget_usd: 5\n", "2026-01-05T00:00:00")
    assert stats_ingest.resolve_provider_profile_sha(repo, "claude-code", "2026-01-01T00:00:00") is None


def test_resolve_provider_profile_sha_is_none_for_a_profile_the_repo_never_carried(tmp_path):
    repo, _ = _make_cartridges_repo(tmp_path, "claude-code", "budget_usd: 5\n", "2026-01-01T00:00:00")
    assert stats_ingest.resolve_provider_profile_sha(repo, "vendor-x", "2026-01-02T00:00:00") is None


def test_resolve_provider_profile_sha_is_none_with_no_cartridges_repo():
    assert stats_ingest.resolve_provider_profile_sha(None, "claude-code", "2026-01-02T00:00:00") is None


def test_ingest_report_provider_profile_sha_counts_sum_to_runs_ingested(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    repo, sha = _make_cartridges_repo(tmp_path, "claude-code", "budget_usd: 5\n", "2026-01-01T00:00:00")
    _write_run(
        runs_dir, "run1", tasks=[("p1", "t1", {"landed": True})],
        node={"provider_profile": "claude-code"},
        usage={"summary": {"started_at": "2026-01-02T00:00:00"}},
    )
    _write_run(
        runs_dir, "run2", tasks=[("p1", "t2", {"landed": True})],
        node={"provider_profile": "claude-code"},
        usage={"summary": {"started_at": "2025-12-01T00:00:00"}},
    )
    db_path = tmp_path / "stats.db"

    report = stats_ingest.ingest(runs_dir, db_path, cartridges_repo=repo)

    assert report.provider_profile_sha_resolved == 1
    assert report.provider_profile_sha_unresolved == 1
    assert (
        report.provider_profile_sha_resolved + report.provider_profile_sha_unresolved == report.runs_ingested
    )
    conn = connect(db_path)
    rows = dict(conn.execute("SELECT run_id, provider_profile_sha FROM runs ORDER BY run_id").fetchall())
    conn.close()
    assert rows["run1"] == sha
    assert rows["run2"] is None


def _facts(record):
    return stats_ingest.gate_facts(record)


def test_gate_facts_reads_the_handoff_complete_bool_as_yes_or_no():
    assert _facts({"handoff": {"complete": True}})["handoff_verdict"] == "yes"
    assert _facts({"handoff": {"complete": False}})["handoff_verdict"] == "no"


def test_gate_facts_leaves_the_handoff_verdict_none_for_a_non_bool_complete():
    assert _facts({"handoff": {"complete": "maybe"}})["handoff_verdict"] is None


def test_gate_facts_reads_only_complete_for_the_handoff_never_a_verdict_key():
    assert _facts({"handoff": {"verdict": "complete", "complete": False}})["handoff_verdict"] == "no"
    assert _facts({"handoff": {"verdict": "complete"}})["handoff_verdict"] is None


def test_gate_facts_reads_the_charter_review_verdict():
    assert _facts({"review": {"verdict": "revise"}})["charter_verdict"] == "revise"


def test_gate_facts_reads_the_adversary_verdict():
    assert _facts({"adversary": {"verdict": "approve"}})["adversary_verdict"] == "approve"


def test_gate_facts_leaves_the_adversary_verdict_none_for_a_findings_list():
    assert _facts({"adversary": [{"why_wrong": "missed the edge case"}]})["adversary_verdict"] is None


def test_gate_facts_reads_arbiter_verdict_and_sided_with_from_an_arbitration_dict():
    facts = _facts({"arbitration": {"verdict": "revise", "sided_with": "adversary"}})
    assert (facts["arbiter_verdict"], facts["arbiter_sided_with"], facts["arbiter_state"]) == ("revise", "adversary", "ruled")


def test_gate_facts_gives_no_ruled_state_for_an_arbitration_dict_with_no_verdict():
    facts = _facts({"arbitration": {"reasoning": "the fix broke the build"}})
    assert (facts["arbiter_verdict"], facts["arbiter_state"]) == (None, None)


def test_gate_facts_gives_none_for_all_three_arbiter_facts_with_no_arbitration_section():
    facts = _facts({"ticket": "t1"})
    assert (facts["arbiter_verdict"], facts["arbiter_sided_with"], facts["arbiter_state"]) == (None, None, None)


def test_gate_facts_stores_the_arbiter_skip_line_as_a_skipped_state():
    facts = _facts({"arbitration": "arbiter: skipped (both approved)"})
    assert (facts["arbiter_verdict"], facts["arbiter_sided_with"], facts["arbiter_state"]) == (None, None, "skipped")


def test_gate_facts_reads_a_stop_reason_string_as_stopped_and_an_explicit_null_as_not_stopped():
    for reason in ("attempts_exhausted", "budget"):
        facts = _facts({"fix_loop": {"attempts": 3, "stopped": reason}})
        assert (facts["fix_loop_attempts"], facts["fix_loop_stopped"]) == (3, 1)
    facts = _facts({"fix_loop": {"attempts": 1, "stopped": None}})
    assert (facts["fix_loop_attempts"], facts["fix_loop_stopped"]) == (1, 0)


def test_gate_facts_leaves_stopped_none_when_the_fix_loop_dict_has_no_stopped_key_or_an_empty_reason():
    assert _facts({"fix_loop": {"attempts": 2}})["fix_loop_stopped"] is None
    assert _facts({"fix_loop": {"attempts": 2, "stopped": ""}})["fix_loop_stopped"] is None


def test_gate_facts_attempts_agree_with_the_join_identitys_count_wherever_the_record_states_one():
    for fix_loop in ({"attempts": 4}, [{"round": 1}, {"round": 2}]):
        record = {"fix_loop": fix_loop}
        assert _facts(record)["fix_loop_attempts"] == stats_ingest._fix_loop_attempts(record)
    assert _facts({"fix_loop": {"stopped": None}})["fix_loop_attempts"] is None


def test_gate_facts_counts_a_fix_loop_list_of_rounds_as_attempts_with_no_stopped_flag():
    facts = _facts({"fix_loop": [{"round": 1}, {"round": 2}]})
    assert (facts["fix_loop_attempts"], facts["fix_loop_stopped"]) == (2, None)


def test_gate_facts_leaves_fix_loop_none_when_absent_or_an_empty_list():
    for record in ({}, {"fix_loop": []}):
        facts = _facts(record)
        assert (facts["fix_loop_attempts"], facts["fix_loop_stopped"]) == (None, None)


def test_gate_facts_reads_the_plan_gate_verdict_from_a_dict_or_a_bare_string():
    assert _facts({"plan_gate": {"verdict": "pass"}})["plan_gate_verdict"] == "pass"
    assert _facts({"plan_gate": "pass"})["plan_gate_verdict"] == "pass"
    assert _facts({"plan_gate": ""})["plan_gate_verdict"] is None


def test_gate_facts_of_an_empty_record_is_nine_nones():
    facts = _facts({})
    assert len(facts) == 9
    assert set(facts.values()) == {None}


def test_a_since_date_filter_joins_tasks_to_runs_started_at(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(
        runs_dir, "run1",
        tasks=[("p1", "t1", {"ticket": "t1"})],
        usage={"summary": {"started_at": "2026-09-20T00:00:00Z"}, "calls": []},
    )
    db_path = tmp_path / "stats.db"
    stats_ingest.ingest(runs_dir, db_path)
    conn = connect(db_path)
    rows = conn.execute(
        "SELECT t.task_id FROM tasks t JOIN runs r ON r.run_id = t.run_id WHERE r.started_at >= '2026-09-01'"
    ).fetchall()
    conn.close()
    assert rows == [("run1:p1:t1",)]


def test_a_fresh_ingest_stamps_schema_version_2(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", {})])
    db_path = tmp_path / "stats.db"
    stats_ingest.ingest(runs_dir, db_path)
    conn = connect(db_path)
    assert conn.execute("SELECT schema_version FROM runs").fetchone() == (2,)
    conn.close()


_GATE_RECORD = {
    "ticket": "t1",
    "review": {"verdict": "approve"},
    "arbitration": "arbiter: skipped (both approved)",
    "fix_loop": {"attempts": 2, "stopped": "attempts_exhausted"},
}


def test_an_old_schema_db_is_upgraded_then_filled_by_a_second_ingest_run(tmp_path):
    db_path = tmp_path / "stats.db"
    old = sqlite3.connect(str(db_path))
    old.execute("CREATE TABLE tasks (run_id TEXT, task_id TEXT, ticket TEXT, outcome TEXT)")
    old.execute("INSERT INTO tasks VALUES ('run1', 'run1:p1:t1', 't1', 'unknown')")
    old.commit()
    old.close()

    conn = connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    stale = conn.execute("SELECT charter_verdict, arbiter_state, fix_loop_attempts FROM tasks").fetchall()
    conn.close()
    assert {
        "handoff_verdict", "charter_verdict", "adversary_verdict", "arbiter_verdict", "arbiter_sided_with",
        "arbiter_state", "fix_loop_attempts", "fix_loop_stopped", "plan_gate_verdict",
    } <= columns
    assert stale == [(None, None, None)]

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", _GATE_RECORD)])
    stats_ingest.ingest(runs_dir, db_path)
    conn = connect(db_path)
    filled = conn.execute("SELECT charter_verdict, arbiter_state, fix_loop_attempts, fix_loop_stopped FROM tasks").fetchall()
    conn.close()
    assert filled == [("approve", "skipped", 2, 1)]


def test_a_rerun_over_the_same_gate_records_creates_no_duplicate_task_rows(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run1", tasks=[("p1", "t1", _GATE_RECORD)])
    db_path = tmp_path / "stats.db"
    stats_ingest.ingest(runs_dir, db_path)
    stats_ingest.ingest(runs_dir, db_path)
    conn = connect(db_path)
    rows = conn.execute("SELECT charter_verdict, arbiter_state FROM tasks").fetchall()
    conn.close()
    assert rows == [("approve", "skipped")]
