import sqlite3

from agent_tools.dash_history_store import read_history_rows


def _store(runs_dir):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT, launched_at TEXT, ended_at TEXT)")
    conn.execute("CREATE TABLE task_records (run_id TEXT, phase_id TEXT, task_id TEXT, record_json TEXT)")
    conn.execute("CREATE TABLE node_calls (run_id TEXT, task_id TEXT, role TEXT, cost_usd REAL, ts TEXT)")
    conn.execute("INSERT INTO runs VALUES ('r_live', '2026-10-04T09:00:00Z', NULL)")
    conn.execute("INSERT INTO runs VALUES ('r_done', '2026-10-04T08:00:00Z', '2026-10-04T08:30:00Z')")
    for run_id in ("r_live", "r_done"):
        conn.execute("INSERT INTO task_records VALUES (?, 'p1', 't1', '{}')", (run_id,))
        conn.execute("INSERT INTO node_calls VALUES (?, 't1', 'build', 0.5, '2026-10-04T08:10:00Z')", (run_id,))
    conn.commit()
    conn.close()


def test_only_the_ended_run_and_its_task_records_and_calls_come_back(tmp_path):
    _store(tmp_path)
    assert read_history_rows(tmp_path, "2026-10-04T00:00:00Z") == {
        "runs": [{"run_id": "r_done", "launched_at": "2026-10-04T08:00:00Z", "ended_at": "2026-10-04T08:30:00Z"}],
        "task_records": [{"run_id": "r_done", "phase_id": "p1", "task_id": "t1", "record_json": "{}"}],
        "node_calls": [{"run_id": "r_done", "task_id": "t1", "role": "build", "cost_usd": 0.5, "ts": "2026-10-04T08:10:00Z"}],
    }
