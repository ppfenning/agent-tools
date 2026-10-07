"""A phase land marks done only the tasks whose work is in the merged PR, and reports the rest by id."""

from agent_tools import cli, land

ITEMS = [{"id": "a", "status": "approved"}, {"id": "b", "status": "approved"}]
PHASE = {"run": "r", "phase": "p", "initiative": "i"}
RECORDS = [{"run": "r", "task": t, "phase": "p", "initiative": "i", "status": "approved",
            "review": {"verdict": "approve"}, "build": {"patch": "x"}} for t in ("a", "b")]
FACTS = [{"task": "a", "reachable": True, "branch_exists": True, "has_patch": True},
         {"task": "b", "reachable": True, "branch_exists": True, "has_patch": True}]


def _plan():
    return land.land_plan(PHASE, {"epic/i/p": []}, "main", items=ITEMS, task_records=RECORDS, task_facts=FACTS)


def test_mark_done_marks_only_the_merged_tasks_and_changes_no_argument():
    tasks, merged = ["a", "b", "c"], {"a", "c"}
    assert land.mark_done(tasks, merged) == ["a", "c"]
    assert tasks == ["a", "b", "c"] and merged == {"a", "c"}


def test_mark_done_with_the_full_set_marks_every_task():
    assert land.mark_done(["a", "b"], {"a", "b"}) == ["a", "b"]


def test_a_phase_plan_with_every_task_merged_marks_all_and_notes_none():
    steps = _plan()
    assert [s["task"] for s in steps if s["kind"] == "mark_done"] == ["a", "b"]
    assert not [s for s in steps if s.get("unmerged")]


def test_a_phase_plan_marks_only_the_merged_set_and_names_the_other_task(monkeypatch):
    monkeypatch.setattr(land, "phase_landed_tasks", lambda steps: frozenset({"a"}))
    steps = _plan()
    assert [s["task"] for s in steps if s["kind"] == "mark_done"] == ["a"]
    assert [s["reason"] for s in steps if s.get("unmerged")] == ["b: work not in the merged PR; left in current state"]


def test_the_land_walk_prints_the_unmerged_task(capsys, tmp_path):
    note = {"kind": "note", "unmerged": ["b"], "reason": "b: work not in the merged PR; left in current state"}
    rc, reached, _ = cli._land_walk(tmp_path, [note], [note], None, None, "full", False, None, [])
    assert (rc, reached) == (0, [])
    assert capsys.readouterr().out == "b: work not in the merged PR; left in current state\n"
