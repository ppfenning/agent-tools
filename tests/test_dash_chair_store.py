import json
import sqlite3
from datetime import UTC, datetime, timedelta

from agent_tools.dash_chair_store import chair_from_store, read_chair_rows

NOW = datetime(2026, 10, 5, 15, 0, 0, tzinfo=UTC)
OFFSET = timedelta(hours=-4)  # local midnight is 2026-10-05T04:00:00Z
STATUS = {"tick_at": "2026-10-05T14:59:13+00:00", "status": "ok", "current_action": {"kind": "land", "target": "x-1"}}
LEASE = {
    "name": "chair", "holder": "chair-a@mac:123", "epoch": 7,
    "heartbeat_at": "2026-10-05T14:59:30+00:00", "expires_at": "2026-10-05T15:05:00+00:00", "status": json.dumps(STATUS),
}
ROWS = [
    {"kind": "land", "ts": "2026-10-05T10:00:00+00:00", "status": "landed"},
    {"kind": "land_phase", "ts": "2026-10-05T11:00:00+00:00", "status": "landed"},
    {"kind": "relaunch", "ts": "2026-10-05T12:00:00+00:00", "status": "done"},
    {"kind": "land", "ts": "2026-10-05T13:00:00+00:00", "status": "refused"},
    {"kind": "land", "ts": "2026-10-05T02:00:00+00:00", "status": "landed"},  # yesterday, Eastern
]
TODAY = {"lands": 2, "launches": 1, "refused_or_failed": 1, "needs_chair_open": 2}


def test_a_lease_row_and_action_rows_build_the_chair_object():
    assert chair_from_store(LEASE, ROWS, 2, NOW, OFFSET) == {
        "holder": "chair-a", "host": "mac", "epoch": 7, "liveness": "live", "beat_age_s": 30,
        "last_tick_at": "2026-10-05T14:59:13+00:00", "last_status": "ok",
        "current_action": {"kind": "land", "target": "x-1"}, "tick_age_s": 47, "today": TODAY,
    }


def test_postgres_datetime_cells_build_the_same_chair_object():
    lease = {**LEASE, "heartbeat_at": datetime(2026, 10, 5, 14, 59, 30, tzinfo=UTC), "expires_at": datetime(2026, 10, 5, 15, 5, tzinfo=UTC)}
    rows = [{**row, "ts": datetime.fromisoformat(row["ts"])} for row in ROWS]
    assert chair_from_store(lease, rows, 2, NOW, OFFSET) == chair_from_store(LEASE, ROWS, 2, NOW, OFFSET)


def test_a_released_lease_reads_none_not_stale():
    assert chair_from_store({**LEASE, "expires_at": "1970-01-01T00:00:00+00:00"}, [], 0, NOW, OFFSET)["liveness"] == "none"


def test_tick_age_is_the_whole_seconds_since_tick_at():
    assert chair_from_store(LEASE, [], 0, NOW, OFFSET)["tick_age_s"] == 47


def test_tick_age_is_none_without_a_tick_at():
    status = json.dumps({"status": "ok", "current_action": None})
    chair = chair_from_store({**LEASE, "status": status}, [], 0, NOW, OFFSET)
    assert (chair["tick_age_s"], chair["last_tick_at"], chair["last_status"], chair["current_action"]) == (None, None, "ok", None)


def test_a_null_or_malformed_status_gives_none_tick_fields_and_keeps_the_lease_fields():
    for status in (None, "", "{not json"):
        chair = chair_from_store({**LEASE, "status": status}, [], 0, NOW, OFFSET)
        ticks = (chair["last_tick_at"], chair["last_status"], chair["current_action"], chair["tick_age_s"])
        assert ticks == (None, None, None, None)
        assert (chair["holder"], chair["host"], chair["epoch"], chair["beat_age_s"]) == ("chair-a", "mac", 7, 30)


def test_the_edge_returns_none_with_no_store(tmp_path):
    assert read_chair_rows(tmp_path / "missing", NOW, OFFSET) is None


def _store(tmp_path, *, actions=True):
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT, status TEXT)")
    conn.execute("INSERT INTO leases VALUES ('chair', ?, 7, ?, ?, ?)", (LEASE["holder"], LEASE["heartbeat_at"], LEASE["expires_at"], LEASE["status"]))
    if actions:
        conn.execute("CREATE TABLE chair_actions (kind TEXT, ts TEXT, action_json TEXT)")
        conn.executemany("INSERT INTO chair_actions VALUES (?, ?, ?)", [
            ("land", "2026-10-05T10:00:00Z", '{"status": "landed"}'),
            ("land_phase", "2026-10-05 11:00:00+00:00", '{"status": "landed"}'),
            ("relaunch", "2026-10-05T08:00:00-04:00", '{"status": "done"}'),
            ("land", "2026-10-05T09:00:00-04:00", '{"status": "refused"}'),
            ("land", "2026-10-04T10:00:00Z", '{"status": "landed"}'),
        ])
    conn.commit()
    conn.close()


def test_the_edge_rows_in_any_iso_form_build_the_chair_object(tmp_path):
    _store(tmp_path)
    lease, rows = read_chair_rows(tmp_path, NOW, OFFSET)
    assert chair_from_store(lease, rows, 2, NOW, OFFSET) == chair_from_store(LEASE, ROWS, 2, NOW, OFFSET)


def test_the_edge_says_why_it_falls_back(tmp_path, capsys):
    _store(tmp_path, actions=False)
    assert read_chair_rows(tmp_path, NOW, OFFSET) is None
    assert "no such table: chair_actions" in capsys.readouterr().err
