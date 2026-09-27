import sqlite3

from agent_tools.chair_read_exits import exits, run_exited

ENDED = "2026-09-27T05:00:00+00:00"


def _run(run_id: str, launched_at: str, ended_at: str | None = None, status: str | None = None) -> dict:
    return {"run_id": run_id, "launched_at": launched_at, "ended_at": ended_at, "status": status}


def test_newest_run_with_an_ended_at_is_exited():
    assert run_exited([_run("i-2", "2026-09-27T04:00", ENDED, "ok")]) == {"i": True}


def test_newest_run_with_status_quarantined_is_exited():
    assert run_exited([_run("i-2", "2026-09-27T04:00", None, "quarantined")]) == {"i": True}


def test_newest_run_still_running_is_not_exited():
    assert run_exited([_run("i-2", "2026-09-27T04:00")]) == {"i": False}


def test_older_exited_run_under_a_newer_running_one_is_not_exited():
    rows = [_run("i-3", "2026-09-27T06:00"), _run("i-2", "2026-09-27T04:00", ENDED, "ok")]
    assert run_exited(rows) == {"i": False}


def test_initiatives_are_told_apart_by_run_id_prefix():
    rows = [_run("a-1", "2026-09-27T04:00", ENDED, "ok"), _run("b-1", "2026-09-27T04:00")]
    assert run_exited(rows) == {"a": True, "b": False}


def test_no_run_row_leaves_the_initiative_absent():
    assert run_exited([]) == {}


def test_edge_reads_the_runs_table_and_reads_a_missing_store_as_empty(tmp_path):
    assert exits(tmp_path) == {}
    conn = sqlite3.connect(tmp_path / "cox.db")
    conn.execute("CREATE TABLE runs (run_id TEXT, launched_at TEXT, ended_at TEXT)")
    conn.execute("INSERT INTO runs VALUES ('i-2', '2026-09-27T04:00', ?)", (ENDED,))
    conn.commit()
    conn.close()
    assert exits(tmp_path) == {"i": True}
