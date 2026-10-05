import json
import sqlite3
from datetime import UTC, datetime

from agent_tools import run_store
from agent_tools.chair_tick_status import build_tick_status, dump_tick_status, write_tick_status

NOW = datetime(2026, 10, 5, 4, 12, 0, tzinfo=UTC)
ACTION = {"kind": "housekeeping", "target": "runs", "since": "2026-10-05T04:10:00Z"}
LINE = "tick 7: nothing to do"


def _store(tmp_path, epoch=3):
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute(
        "CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT, status TEXT)"
    )
    conn.execute("INSERT INTO leases VALUES ('chair', 's@h:1', ?, '2026-10-05T04:11:00Z', '2026-10-05T04:16:00Z', NULL)", (epoch,))
    conn.commit()
    conn.close()


def _status(tmp_path):
    conn = sqlite3.connect(tmp_path / "cox.db")
    try:
        return conn.execute("SELECT status FROM leases WHERE name = 'chair'").fetchone()[0]
    finally:
        conn.close()


def test_the_builder_returns_the_tick_dict():
    assert build_tick_status(LINE, NOW, ACTION) == {
        "tick_at": "2026-10-05T04:12:00Z",
        "status": "tick 7: nothing to do",
        "current_action": {"kind": "housekeeping", "target": "runs", "since": "2026-10-05T04:10:00Z"},
    }


def test_the_builder_with_no_action_gives_current_action_none():
    assert build_tick_status(LINE, NOW, None)["current_action"] is None


def test_the_json_text_round_trips():
    status = build_tick_status(LINE, NOW, ACTION)
    assert json.loads(dump_tick_status(status)) == status


def test_the_read_only_open_refuses_an_update(tmp_path):
    _store(tmp_path)
    conn, _ = run_store._open(tmp_path)
    try:
        refused = False
        try:
            conn.execute("UPDATE leases SET status = 'x'")
        except sqlite3.OperationalError:
            refused = True
    finally:
        conn.close()
    assert refused


def test_the_edge_updates_the_row_at_the_held_epoch(tmp_path):
    _store(tmp_path, epoch=3)
    assert write_tick_status(tmp_path, 3, '{"status":"ok"}') is True
    assert _status(tmp_path) == '{"status":"ok"}'


def test_the_edge_leaves_a_row_at_another_epoch_unchanged(tmp_path):
    _store(tmp_path, epoch=3)
    assert write_tick_status(tmp_path, 4, '{"status":"ok"}') is False
    assert _status(tmp_path) is None


def test_the_edge_with_no_store_returns_false(tmp_path):
    assert write_tick_status(tmp_path, 3, "{}") is False
    assert not (tmp_path / "cox.db").exists()
