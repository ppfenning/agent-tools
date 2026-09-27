from agent_tools import run_store
from agent_tools.chair_read_quarantined import (
    attempts_on_current_body,
    body_sha,
    quarantined_from_rows,
    quarantined_rows,
    read_quarantined,
)

BODY = "body\n"
ON_BODY = {"run": "i-1", "ts": "2026-09-25T01:00:00Z", "body_sha": body_sha(BODY)}
PATH = "work/a/p1/t1.md"


def test_a_ready_item_with_one_attempt_on_its_body_is_a_quarantine_row_with_that_run():
    assert quarantined_rows([(PATH, "ready", BODY, [ON_BODY])]) == [{"initiative": "a", "phase": "p1", "task": "t1", "run": "i-1"}]


def test_an_attempt_whose_body_sha_differs_gives_no_row():
    assert quarantined_rows([(PATH, "ready", BODY, [{**ON_BODY, "body_sha": "0" * 12}])]) == []


def test_a_row_carries_the_run_of_the_newest_attempt():
    newer = {**ON_BODY, "run": "i-2", "ts": "2026-09-26T01:00:00Z"}
    assert quarantined_rows([(PATH, "blocked", BODY, [newer, ON_BODY])])[0]["run"] == "i-2"


def test_an_attempt_with_no_body_sha_counts_as_on_the_body():
    assert attempts_on_current_body([{"run": "i-1"}, {"run": "i-2", "body_sha": "0" * 12}], BODY) == [{"run": "i-1"}]


def test_the_body_sha_is_twelve_hex_digits_of_the_stripped_body():
    assert body_sha("  hi\n") == "8f434346648f"


def test_an_item_in_any_other_state_gives_no_row():
    assert [quarantined_rows([(PATH, state, BODY, [ON_BODY])]) for state in ("quarantined", "done", "approved")] == [[]] * 3


def test_a_path_not_shaped_work_initiative_phase_task_md_gives_no_row():
    items = [("work/a/t1.md", "ready", BODY, [ON_BODY]), ("work/a/p1/t1.txt", "ready", BODY, [ON_BODY])]
    assert quarantined_rows(items) == []


def _world(tmp_path, monkeypatch, store_rows):
    item = tmp_path / "work" / "a" / "p1" / "t1.md"
    item.parent.mkdir(parents=True)
    item.write_text(f"---\nstate: ready\nattempts:\n  - run: i-1\n    ts: t\n    body_sha: {body_sha(BODY)}\n---\n{BODY}")
    calls = []
    monkeypatch.setattr(run_store, "work_items", lambda runs_dir, initiative=None: calls.append(runs_dir) or store_rows)
    return calls


def test_under_files_mode_the_file_state_holds_and_the_store_is_not_read(tmp_path, monkeypatch):
    calls = _world(tmp_path, monkeypatch, [{"initiative": "a", "task_id": "t1", "state": "done"}])
    row = {"initiative": "a", "phase": "p1", "task": "t1", "run": "i-1"}
    assert (read_quarantined(tmp_path, "files"), calls) == ([row], [])


def test_under_store_mode_a_done_store_row_hides_a_file_once_quarantined(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch, [{"initiative": "a", "task_id": "t1", "state": "done"}])
    assert read_quarantined(tmp_path, "store") == []


ROW = {
    "kind": "task", "initiative": "a", "task_id": "t1", "phase": "p1", "state": "quarantined",
    "needs": [], "title": "", "surfaces": [], "body": BODY,
    "extra": {"attempts": [{"run": "i-1", "ts": "2026-09-25T01:00:00Z", "cause": "harness", "body_sha": body_sha(BODY)}]},
}


def test_a_quarantined_row_gives_its_fact_with_body_sha_and_cause_from_the_newest_attempt():
    assert quarantined_from_rows([ROW]) == [
        {"initiative": "a", "phase": "p1", "task": "t1", "run": "i-1", "body_sha": body_sha(BODY), "cause": "harness"}
    ]


def test_a_row_in_any_other_state_gives_no_fact():
    assert quarantined_from_rows([{**ROW, "state": "ready"}]) == []
