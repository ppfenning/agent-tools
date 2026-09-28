import sqlite3

from agent_tools.chair_read_remote_unfetched import read_remote_unfetched

RUNS_COLUMNS = "run_id TEXT PRIMARY KEY, launched_at TEXT"


def _runs_table(runs_dir, *run_ids):
    conn = sqlite3.connect(runs_dir / "cox.db")
    conn.execute(f"CREATE TABLE runs ({RUNS_COLUMNS})")
    for run_id in run_ids:
        conn.execute("INSERT INTO runs (run_id, launched_at) VALUES (?, ?)", (run_id, "2026-09-25T00:00:00Z"))
    conn.commit()
    conn.close()


def test_remote_record_with_no_local_run_dir_or_log_is_present_with_its_run_id(tmp_path):
    _runs_table(tmp_path, "i-1")
    (tmp_path / "i-1.remote.json").write_text("{}", encoding="utf-8")
    assert read_remote_unfetched(tmp_path, ["i"]) == {"i": "i-1"}


def test_remote_record_with_a_local_run_dir_is_absent(tmp_path):
    _runs_table(tmp_path, "i-1")
    (tmp_path / "i-1.remote.json").write_text("{}", encoding="utf-8")
    (tmp_path / "i-1").mkdir()
    assert read_remote_unfetched(tmp_path, ["i"]) == {}


def test_remote_record_with_a_local_log_and_no_run_dir_is_absent(tmp_path):
    _runs_table(tmp_path, "i-1")
    (tmp_path / "i-1.remote.json").write_text("{}", encoding="utf-8")
    (tmp_path / "i-1.log").write_text("", encoding="utf-8")
    assert read_remote_unfetched(tmp_path, ["i"]) == {}


def test_no_remote_record_is_absent(tmp_path):
    _runs_table(tmp_path, "i-1")
    assert read_remote_unfetched(tmp_path, ["i"]) == {}


def test_an_older_remote_unfetched_run_is_reported_despite_a_newer_local_run(tmp_path):
    _runs_table(tmp_path, "i-2", "i-3")
    (tmp_path / "i-2.remote.json").write_text("{}", encoding="utf-8")
    (tmp_path / "i-3").mkdir()
    assert read_remote_unfetched(tmp_path, ["i"]) == {"i": "i-2"}
