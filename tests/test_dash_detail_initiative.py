"""`dash_detail_initiative.shape`, `.build` and `.build_initiative_detail` for
`cox dash --detail initiative`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from agent_tools import dash_detail_initiative as ddi
from agent_tools import run_store

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dash_detail_initiative_v1.json"

_INITIATIVE = "dash-feed-streams-a-versioned-json-snapshot-of"
_SCHEMA = f"{_INITIATIVE}-feed-schema"
_WRITER = f"{_INITIATIVE}-feed-writer"
_CLI = f"{_INITIATIVE}-cli-flag"

# Store items `shape` consumes: p1-foundations' writer needs its own phase's
# schema task, proving the same-phase error; p2-cli's flag needs the p1 schema
# task, proving a cross-phase edge is not flagged.
_V1_ITEMS = [
    {"id": _SCHEMA, "phase": "p1-foundations", "state": "done", "needs": [], "updated_at": "2026-09-28T21:00:00Z"},
    {
        "id": _WRITER,
        "phase": "p1-foundations",
        "state": "done",
        "needs": [_SCHEMA],
        "updated_at": "2026-09-28T22:10:00Z",
    },
    {"id": _CLI, "phase": "p2-cli", "state": "ready", "needs": [_SCHEMA], "updated_at": "2026-09-29T09:00:00Z"},
]
_V1_RUNS = [{"run": f"{_INITIATIVE}-1", "cost_usd": 0.75}]

# The literal raw input: what `shape` emits today for the items above.
_RAW_INITIATIVE = {
    "generated_at": "2026-09-29T12:00:00Z",
    "phases": [
        {"name": "p1-foundations", "landed_at": "2026-09-28T22:10:00Z"},
        {"name": "p2-cli", "landed_at": None},
    ],
    "needs": [{"from": _SCHEMA, "to": _CLI}, {"from": _SCHEMA, "to": _WRITER}],
    "tasks": [
        {"id": _SCHEMA, "phase": "p1-foundations", "state": "done"},
        {"id": _WRITER, "phase": "p1-foundations", "state": "done"},
        {"id": _CLI, "phase": "p2-cli", "state": "ready"},
    ],
    "runs": [{"run": f"{_INITIATIVE}-1", "cost_usd": 0.75}],
}

_ITEMS = [
    {"id": "t1", "phase": "p1", "state": "done", "needs": [], "updated_at": "2026-09-20T00:00:00Z"},
    {"id": "t2", "phase": "p1", "state": "dropped", "needs": [], "updated_at": "2026-09-21T00:00:00Z"},
    {"id": "t3", "phase": "p2", "state": "todo", "needs": ["t1"], "updated_at": "2026-09-22T00:00:00Z"},
]
_RUNS = [
    {"run": "demo-2", "cost_usd": 2.0},
    {"run": "demo-1", "cost_usd": 1.5},
]
_EXPECTED = {
    "generated_at": "2026-09-28T00:00:00Z",
    "phases": [
        {"name": "p1", "landed_at": "2026-09-21T00:00:00Z"},
        {"name": "p2", "landed_at": None},
    ],
    "needs": [{"from": "t1", "to": "t3"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "state": "done"},
        {"id": "t2", "phase": "p1", "state": "dropped"},
        {"id": "t3", "phase": "p2", "state": "todo"},
    ],
    "runs": [
        {"run": "demo-1", "cost_usd": 1.5},
        {"run": "demo-2", "cost_usd": 2.0},
    ],
}

_NODE_CALLS_TABLE = (
    "CREATE TABLE node_calls (call_id TEXT PRIMARY KEY, run_id TEXT, seq INTEGER, task_id TEXT, role TEXT, "
    "tier TEXT, model_alias TEXT, cost_usd REAL, ceiling_usd REAL, ceiling_source TEXT, turns INTEGER, "
    "duration_ms INTEGER, input_tokens INTEGER, cache_read_tokens INTEGER, cache_creation_tokens INTEGER, "
    "input_total INTEGER, output_tokens INTEGER, ok BOOL, ts TEXT, decision_json TEXT, detail_json TEXT)"
)
_RUNS_TABLE = "CREATE TABLE runs (run_id TEXT PRIMARY KEY, ended_at TEXT)"


def _node_call(conn, call_id: str, run_id: str, cost_usd: float, ts: str) -> None:
    conn.execute(
        "INSERT INTO node_calls (call_id, run_id, seq, role, cost_usd, turns, ts) VALUES (?, ?, 1, 'worker', ?, 4, ?)",
        (call_id, run_id, cost_usd, ts),
    )


def test_shape_builds_phases_needs_tasks_and_runs_from_literal_lists() -> None:
    assert ddi.shape(_ITEMS, _RUNS, "2026-09-28T00:00:00Z") == _EXPECTED


def test_build_wires_the_work_store_and_run_record_readers_through_shape(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ddi, "_items", lambda runs_dir, initiative_id: _ITEMS)
    monkeypatch.setattr(ddi, "_runs", lambda runs_dir, initiative_id: _RUNS)

    result = ddi.build("demo", tmp_path / "work", tmp_path / "runs", "2026-09-28T00:00:00Z")

    assert result == _EXPECTED


def test_items_reads_task_id_state_needs_and_updated_at_from_the_store(tmp_path, monkeypatch) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE work_items (initiative, task_id, phase, state, needs_json, updated_at, updated_by)")
    conn.executemany(
        "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?, 'me')",
        [
            ("demo", "t1", "p1", "done", "[]", "2026-09-20T00:00:00Z"),
            ("demo", "t2", "p1", "dropped", "[]", "2026-09-21T00:00:00Z"),
            ("demo", "t3", "p2", "todo", '["t1"]', "2026-09-22T00:00:00Z"),
            ("other", "x1", "p1", "done", "[]", "2026-09-01T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(run_store, "_harness_python", lambda: Path(sys.executable))
    monkeypatch.setattr(run_store, "_store_url", lambda runs_dir: f"sqlite:///{db}")

    items = ddi._items(tmp_path, "demo")

    assert items == _ITEMS
    # A landed phase with two tasks must not raise: `updated_at` came from the
    # store, not from absent frontmatter, so `max()` sees real strings.
    assert ddi.shape(items, [], "2026-09-28T00:00:00Z")["phases"] == [
        {"name": "p1", "landed_at": "2026-09-21T00:00:00Z"},
        {"name": "p2", "landed_at": None},
    ]


def test_runs_reads_local_usage_files_and_filters_by_initiative(tmp_path) -> None:
    (tmp_path / "demo-1.usage.json").write_text(
        json.dumps({"calls": [{"cost_usd": 1.5, "role": "worker", "turns": 2}]}), encoding="utf-8"
    )
    (tmp_path / "demo-2.usage.json").write_text(
        json.dumps({"calls": [{"cost_usd": 0.5, "role": "worker", "turns": 1}]}), encoding="utf-8"
    )
    (tmp_path / "other-1.usage.json").write_text(
        json.dumps({"calls": [{"cost_usd": 9.0, "role": "worker", "turns": 5}]}), encoding="utf-8"
    )

    runs = ddi._runs(tmp_path, "demo")

    assert sorted(runs, key=lambda row: row["run"]) == [
        {"run": "demo-1", "cost_usd": 1.5},
        {"run": "demo-2", "cost_usd": 0.5},
    ]


def test_runs_also_counts_a_run_recorded_only_in_the_store(tmp_path) -> None:
    (tmp_path / "demo-1.usage.json").write_text(
        json.dumps({"calls": [{"cost_usd": 1.5, "role": "worker", "turns": 2}]}), encoding="utf-8"
    )
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute(_NODE_CALLS_TABLE)
    conn.execute(_RUNS_TABLE)
    _node_call(conn, "c1", "demo-9", 3.0, "2026-09-25T00:00:00Z")
    conn.execute("INSERT INTO runs (run_id, ended_at) VALUES ('demo-9', '2026-09-25T00:05:00Z')")
    _node_call(conn, "c2", "other-5", 9.0, "2026-09-25T00:00:00Z")
    conn.execute("INSERT INTO runs (run_id, ended_at) VALUES ('other-5', '2026-09-25T00:05:00Z')")
    conn.commit()
    conn.close()

    runs = ddi._runs(tmp_path, "demo")

    assert sorted(runs, key=lambda row: row["run"]) == [
        {"run": "demo-1", "cost_usd": 1.5},
        {"run": "demo-9", "cost_usd": 3.0},
    ]


def test_raw_initiative_literal_is_what_shape_emits_today() -> None:
    assert ddi.shape(_V1_ITEMS, _V1_RUNS, "2026-09-29T12:00:00Z") == _RAW_INITIATIVE


def test_build_initiative_detail_matches_the_v1_fixture() -> None:
    expected = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))

    result = ddi.build_initiative_detail(_RAW_INITIATIVE, _INITIATIVE)

    assert result == expected
    assert json.loads(json.dumps(result)) == result


def test_build_initiative_detail_flags_only_the_same_phase_needs_edge() -> None:
    result = ddi.build_initiative_detail(_RAW_INITIATIVE, _INITIATIVE)

    p1_tasks, p2_tasks = result["phases"][0]["tasks"], result["phases"][1]["tasks"]
    assert p1_tasks[0]["error"] is None
    assert p1_tasks[1]["error"] == "needs edge inside phase p1-foundations"
    assert p2_tasks[0]["needs"] == [_SCHEMA]
    assert p2_tasks[0]["error"] is None
