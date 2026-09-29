import json
import sqlite3
from datetime import UTC, datetime

from agent_tools import run_store
from agent_tools.chair_read_stall import read_stall_candidates

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
LAUNCHED_AT = "2026-09-29T10:00:00Z"
LIVE_EXPIRES = "2026-09-29T23:00:00Z"

_CALL = {"run_id": "abc-1", "seq": 1, "task_id": "t1", "role": "build", "ts": "2026-09-29T11:45:00Z"}


def _live_store(tmp_path, run_id="abc-1", launched_at=LAUNCHED_AT):
    """A `runs` row and a live `leases` row for `run_id`, plus a pidfile: the harness's own shape for a run
    whose process is still alive, the case `run_store.usage()` could never see a call for."""
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT, launched_at TEXT, ended_at TEXT)")
    conn.execute("INSERT INTO runs VALUES (?, ?, NULL)", (run_id, launched_at))
    conn.execute("CREATE TABLE leases (name TEXT, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT)")
    conn.execute(
        "INSERT INTO leases VALUES (?, ?, 1, ?, ?)", (run_store._lease_name(run_id), run_id, launched_at, LIVE_EXPIRES)
    )
    conn.commit()
    conn.close()
    (tmp_path / f"{run_id}.pid").write_text("12345")


def test_a_live_run_with_no_remote_json_gives_local_true(tmp_path):
    _live_store(tmp_path)
    candidates = read_stall_candidates(tmp_path, (), NOW)
    assert candidates[0]["local"] is True
    assert candidates[0]["started_at"] == LAUNCHED_AT


def test_a_live_run_with_a_remote_json_naming_jarvis_gives_local_false(tmp_path):
    _live_store(tmp_path)
    (tmp_path / "abc-1.remote.json").write_text(json.dumps({"host": "jarvis", "launched_at": LAUNCHED_AT}))
    candidates = read_stall_candidates(tmp_path, (), NOW)
    assert candidates[0]["local"] is False


def test_a_stalled_usr1_row_targeting_the_run_gives_usr1_sent_true(tmp_path):
    _live_store(tmp_path)
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE chair_actions (kind TEXT, target TEXT, ts TEXT)")
    conn.execute("INSERT INTO chair_actions VALUES ('stalled_usr1', 'abc-1', '2026-09-29T11:00:00Z')")
    conn.commit()
    conn.close()
    candidates = read_stall_candidates(tmp_path, (), NOW)
    assert candidates[0]["usr1_sent"] is True


def test_no_stalled_usr1_row_gives_usr1_sent_false(tmp_path):
    _live_store(tmp_path)
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE chair_actions (kind TEXT, target TEXT, ts TEXT)")
    conn.execute("INSERT INTO chair_actions VALUES ('housekeeping', 'abc-1', '2026-09-29T11:00:00Z')")
    conn.commit()
    conn.close()
    candidates = read_stall_candidates(tmp_path, (), NOW)
    assert candidates[0]["usr1_sent"] is False


def test_a_run_with_one_node_calls_row_gives_last_call_with_its_role_task_and_ts(tmp_path):
    _live_store(tmp_path)
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE node_calls (" + ", ".join(_CALL) + ")")
    marks = ", ".join("?" * len(_CALL))
    conn.execute("INSERT INTO node_calls VALUES (" + marks + ")", tuple(_CALL.values()))
    conn.commit()
    conn.close()
    candidates = read_stall_candidates(tmp_path, (), NOW)
    assert candidates[0]["last_call"] == {"role": "build", "task": "t1", "ts": "2026-09-29T11:45:00Z"}


def test_a_run_with_no_node_calls_rows_gives_last_call_none(tmp_path):
    _live_store(tmp_path)
    candidates = read_stall_candidates(tmp_path, (), NOW)
    assert candidates[0]["last_call"] is None
