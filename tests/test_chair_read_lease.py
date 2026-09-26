import sqlite3
from datetime import UTC, datetime

from agent_tools import chair
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import lease_facts
from agent_tools.chair_plan import _lease_gate
from agent_tools.chair_read_lease import ABSENT, lease_record, read_lease

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
HOLDER = lease_holder("s", 7, "h")


def test_a_held_lease_inside_its_expiry_is_neither_stale_nor_released():
    row = {"holder": HOLDER, "epoch": 3, "expires_at": "2026-09-26T12:05:00Z"}
    assert lease_record(row, NOW) == {"holder": HOLDER, "host": "h", "epoch": 3, "released": False, "stale": False}


def test_an_expired_lease_is_stale():
    assert lease_record({"holder": HOLDER, "epoch": 3, "expires_at": "2026-09-26T11:59:00Z"}, NOW)["stale"] is True


def test_a_released_lease_is_released():
    assert lease_record({"holder": HOLDER, "epoch": 3, "expires_at": "1970-01-01T00:00:00Z"}, NOW)["released"] is True
    assert lease_record({"holder": None, "epoch": None, "expires_at": None}, NOW)["released"] is True


def test_no_lease_plans_a_take():
    assert lease_record(None, NOW) == ABSENT
    assert _lease_gate(lease_facts(lease_record(None, NOW), "s", 7, "h")) == [{"kind": "take_lease"}]


def _store(runs_dir, *rows):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute("CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, epoch INTEGER, heartbeat_at TEXT, expires_at TEXT)")
    conn.executemany("INSERT INTO leases VALUES (?, ?, ?, '2026-09-26T11:59:00Z', ?)", rows)
    conn.commit()
    conn.close()


def test_the_edge_reads_the_chair_row_and_ignores_others(tmp_path):
    _store(tmp_path, ("runs:x", "x-1", 1, "2026-09-26T13:00:00Z"), ("chair", HOLDER, 4, "2026-09-26T12:05:00Z"))
    assert read_lease(tmp_path, NOW) == {"holder": HOLDER, "host": "h", "epoch": 4, "released": False, "stale": False}


def test_a_store_whose_leases_table_has_no_epoch_column_still_reads_the_held_lease_with_epoch_zero(tmp_path):
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE leases (name TEXT PRIMARY KEY, holder TEXT, heartbeat_at TEXT, expires_at TEXT)")
    conn.execute("INSERT INTO leases VALUES ('chair', ?, '2026-09-26T11:59:00Z', '2026-09-26T12:05:00Z')", (HOLDER,))
    conn.commit()
    conn.close()
    assert read_lease(tmp_path, NOW) == {"holder": HOLDER, "host": "h", "epoch": 0, "released": False, "stale": False}


def test_the_edge_reads_absent_with_no_store_or_no_chair_row(tmp_path):
    assert read_lease(tmp_path, NOW) == ABSENT
    _store(tmp_path, ("runs:x", "x-1", 1, "2026-09-26T13:00:00Z"))
    assert read_lease(tmp_path, NOW) == ABSENT


def test_the_edge_adds_until_and_expired_from_the_holders_chair_record(tmp_path):
    _store(tmp_path, ("chair", HOLDER, 4, "2026-09-26T12:05:00Z"))
    chair.write(tmp_path, {"session": "s", "pid": 7, "host": "h", "until": "2026-09-26T11:00:00+00:00"})
    lease = read_lease(tmp_path, NOW)
    assert (lease["until"], lease["expired"]) == ("2026-09-26T11:00:00+00:00", True)
    assert lease_facts(lease, "other", 1, "h")["expired"] is True


def test_the_edge_ignores_the_until_of_another_holders_chair_record(tmp_path):
    _store(tmp_path, ("chair", HOLDER, 4, "2026-09-26T12:05:00Z"))
    chair.write(tmp_path, {"session": "zed", "pid": 7, "host": "h", "until": "2026-09-26T11:00:00+00:00"})
    assert "expired" not in read_lease(tmp_path, NOW)
