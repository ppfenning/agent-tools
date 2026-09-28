import sqlite3

from agent_tools.chair_read_attempts import (
    attempt_rows,
    current_body_starts,
    fill_causes,
    path_parts,
    read_attempts,
    with_stored_rescues,
)
from agent_tools.chair_read_quarantined import body_sha

PATH = "work/init-a/phase-1/task-x.md"
KEY = ("init-a", "phase-1", "task-x")
STORE = {"run_id": "init-a-1", "task_id": "task-x", "phase_id": "phase-1", "ts": "2026-09-05", "cause": "code"}
RESCUE = {
    "run": "init-a-1",
    "task": "task-x",
    "phase": "phase-1",
    "initiative": "init-a",
    "ts": "2026-09-05",
    "kind": "rescue_failed",
    "cause": "rescue_failed",
    "on_current_body": True,
}


def test_attempts_on_different_items_sort_oldest_first():
    items = [
        (PATH, [{"run": "r2", "ts": "2026-09-02T00:00:00Z"}]),
        ("work/init-b/phase-2/task-y.md", [{"run": "r1", "ts": "2026-09-01T00:00:00Z"}]),
    ]
    assert [row["run"] for row in attempt_rows(items)] == ["r1", "r2"]


def test_a_row_takes_initiative_phase_and_task_from_the_path():
    (row,) = attempt_rows([(PATH, [{"run": "r1", "ts": "t"}])])
    assert (row["initiative"], row["phase"], row["task"]) == ("init-a", "phase-1", "task-x")


def test_an_explicit_initiative_is_kept():
    (row,) = attempt_rows([(PATH, [{"run": "r1", "ts": "t", "initiative": "other"}])])
    assert row["initiative"] == "other"


def test_an_item_with_no_attempts_adds_no_rows():
    assert attempt_rows([(PATH, []), (PATH, None)]) == []


def test_a_missing_cause_is_carried_as_absent():
    (row,) = attempt_rows([(PATH, [{"run": "r1", "ts": "t"}])])
    assert "cause" in row and row["cause"] is None


def test_an_attempt_with_another_bodys_sha_is_off_current_body_and_a_sha_less_one_is_on_it():
    body_a = "body A"
    attempts = [{"run": "r1", "body_sha": body_sha(body_a)}, {"run": "r2"}]
    rows = attempt_rows([(PATH, attempts, "body B")])
    assert {row["run"]: row["on_current_body"] for row in rows} == {"r1": False, "r2": True}


def test_a_path_outside_the_work_layout_has_no_parts():
    assert path_parts("notes/x.md") == (None, None, None)


def test_the_edge_loads_items_from_frontmatter(tmp_path):
    item = tmp_path / "work" / "init-a" / "phase-1" / "task-x.md"
    item.parent.mkdir(parents=True)
    item.write_text("---\nattempts:\n  - run: r1\n    ts: '2026-09-01'\n    cause: flaky\n---\nbody\n")
    (row,) = read_attempts(tmp_path)
    assert (row["run"], row["cause"], row["initiative"], row["path"]) == ("r1", "flaky", "init-a", str(item.relative_to(tmp_path)))


def test_a_file_attempt_with_no_cause_takes_the_store_rows_cause():
    store = [{"run_id": "r1", "task_id": "task-x", "seq": 1, "cause": "harness"}]
    (row,) = fill_causes([{"run": "r1", "task": "task-x", "cause": None}], store)
    assert row["cause"] == "harness"


def test_a_stored_rescue_after_the_first_attempt_is_appended_in_ts_order():
    later = {"run": "init-a-2", "ts": "2026-09-09"}
    assert with_stored_rescues([later], [STORE], {KEY: "2026-09-01"}) == [RESCUE, later]


def test_a_stored_rescue_from_before_the_first_attempt_on_the_current_body_is_dropped():
    assert with_stored_rescues([], [STORE], {KEY: "2026-09-06"}) == []


def test_a_stored_rescue_for_another_task_or_phase_or_with_no_attempt_is_dropped():
    other = {**STORE, "task_id": "task-y"}
    assert with_stored_rescues([], [STORE, other], {("init-a", "phase-2", "task-x"): "2026-09-01"}) == []


def test_the_first_attempt_on_the_current_body_skips_attempts_with_another_body_sha():
    body = "\nnew body\n"
    attempts = [{"ts": "2026-09-01", "body_sha": "old"}, {"ts": "2026-09-07", "body_sha": body_sha(body)}, {"ts": "2026-09-08"}]
    assert current_body_starts([(PATH, body, attempts), (PATH, body, [])]) == {KEY: "2026-09-07"}


def test_the_edge_adds_a_store_rescue_after_the_current_bodys_first_attempt_and_not_one_before_it(tmp_path):
    item = tmp_path / "work" / "init-a" / "phase-1" / "task-x.md"
    item.parent.mkdir(parents=True)
    item.write_text(f"---\nattempts:\n  - run: init-a-1\n    ts: '2026-09-03'\n    body_sha: {body_sha('body')}\n---\nbody\n")
    (tmp_path / "runs").mkdir()
    conn = sqlite3.connect(tmp_path / "runs" / "cox.db")
    conn.execute("CREATE TABLE attempts (run_id TEXT, task_id TEXT, seq INTEGER, phase_id TEXT, kind TEXT, reason TEXT, ts TEXT, cause TEXT)")
    for seq, ts in enumerate(("2026-09-02", "2026-09-05")):
        conn.execute("INSERT INTO attempts (run_id, task_id, seq, phase_id, kind, ts, cause) VALUES ('init-a-1', 'task-x', ?, 'phase-1', 'rescue_failed', ?, 'code')", (seq, ts))
    conn.commit()
    conn.close()
    assert [(r.get("kind"), r["ts"]) for r in read_attempts(tmp_path)] == [(None, "2026-09-03"), ("rescue_failed", "2026-09-05")]


def test_a_file_cause_is_kept_and_the_newest_seq_wins():
    store = [{"run_id": "r1", "task_id": "t", "seq": 2, "cause": "new"}, {"run_id": "r1", "task_id": "t", "seq": 1, "cause": "old"}]
    rows = fill_causes([{"run": "r1", "task": "t", "cause": None}, {"run": "r1", "task": "t", "cause": "file"}], store)
    assert [r["cause"] for r in rows] == ["new", "file"]
