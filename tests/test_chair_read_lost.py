import sqlite3

from agent_tools import run_store
from agent_tools.chair_read_lost import lost_runs_of, read_lost_runs, stale_hosts

NOW = "2026-09-27T12:00:00Z"


def _host(name: str, beat_at: str | None) -> dict:
    return {"name": name, "beat_at": beat_at}


def test_a_host_beaten_eleven_minutes_ago_is_stale():
    assert stale_hosts([_host("h", "2026-09-27T11:49:00Z")], NOW) == {"h"}


def test_a_host_beaten_five_minutes_ago_is_not_stale():
    assert stale_hosts([_host("h", "2026-09-27T11:55:00Z")], NOW) == set()


def test_a_host_with_a_blank_beat_at_is_stale():
    assert stale_hosts([_host("h", "")], NOW) == {"h"}


def test_a_lane_on_a_stale_host_with_no_exit_record_is_lost():
    lane = run_store.Lane("i-2", "h", "2026-09-27T10:00:00Z", "2026-09-27T11:00:00Z")
    assert lost_runs_of([lane], {"h"}, {}, NOW) == {"i": "i-2"}


def test_the_same_lane_is_not_lost_once_its_run_is_exited():
    lane = run_store.Lane("i-2", "h", "2026-09-27T10:00:00Z", "2026-09-27T11:00:00Z")
    assert lost_runs_of([lane], {"h"}, {"i-2": True}, NOW) == {}


def test_a_lane_on_a_stale_host_that_renewed_its_lease_two_minutes_ago_is_not_lost():
    lane = run_store.Lane("i-2", "h", "2026-09-27T10:00:00Z", "2026-09-27T11:58:00Z")
    assert lost_runs_of([lane], {"h"}, {}, NOW) == {}


def test_edge_keys_exits_by_run_and_reports_only_the_unexited_lane_on_the_stale_host(tmp_path):
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE hosts (name TEXT, beat_at TEXT)")
    conn.executemany("INSERT INTO hosts VALUES (?, ?)", [("dead", "2026-09-27T11:49:00Z"), ("fresh", "2026-09-27T11:59:00Z")])
    conn.execute("CREATE TABLE runs (run_id TEXT, launched_at TEXT, ended_at TEXT, status TEXT, host TEXT)")
    conn.executemany(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
        [
            ("i-2", "2026-09-27T10:00:00Z", "2026-09-27T11:30:00Z", "ok", "dead"),
            ("j-1", "2026-09-27T10:00:00Z", None, None, "dead"),
            ("k-1", "2026-09-27T10:00:00Z", None, None, "fresh"),
        ],
    )
    conn.execute("CREATE TABLE leases (name TEXT, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT)")
    conn.executemany(
        "INSERT INTO leases VALUES (?, ?, 1, '2026-09-27T11:40:00Z', '2026-09-27T12:02:00Z')",
        [("runs:i", "i-2"), ("runs:j", "j-1"), ("runs:k", "k-1")],
    )
    conn.commit()
    conn.close()
    assert read_lost_runs(tmp_path, NOW) == {"j": "j-1"}
