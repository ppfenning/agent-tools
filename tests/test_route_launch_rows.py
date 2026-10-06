import builtins
import io
import pathlib

import pytest

from agent_tools import queue_rows, route, run_store

PLAN = {
    "id": "init-a", "title": "A", "body": "about A",
    "phases": [{"id": "p1", "goal": "g1"}, {"id": "p2", "goal": "g2"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "title": "T1", "body": "do one", "needs": [], "surfaces": ["a.py"]},
        {"id": "t2", "phase": "p2", "title": "T2", "body": "do two", "needs": ["t1"], "surfaces": ["b.py", "c.py"]},
    ],
}
INTAKE = {
    "kind": "intake", "initiative": "intake", "task_id": "I1", "phase": "", "state": "queued",
    "needs": [], "title": "Idea", "surfaces": [], "body": "the idea", "extra": {"repo": "/r/a"},
}
HEAD, T1, T2 = queue_rows.plan_to_rows(PLAN, "I1")
STAMPED = queue_rows.intake_stamp(INTAKE, "init-a")


def _task(task_id, phase, state, needs):
    return {**T1, "task_id": task_id, "phase": phase, "state": state, "needs": needs}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace with no work/ and no intake/, where any file open fails the test."""
    monkeypatch.chdir(tmp_path)

    def refuse(*args, **kwargs):
        raise AssertionError(f"file opened: {args[:1]}")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(io, "open", refuse)
    monkeypatch.setattr(pathlib.Path, "open", refuse)
    return tmp_path


def _store(monkeypatch, rows):
    monkeypatch.setattr(
        run_store, "read_queue",
        lambda runs_dir, initiative=None, kind=None: [
            r for r in rows if initiative in (None, r["initiative"]) and kind in (None, r["kind"])
        ],
    )


def test_decompose_rows_give_the_launch_input_with_repo_from_the_intake(workspace, monkeypatch):
    _store(monkeypatch, [HEAD, T1, T2, STAMPED])
    assert route.launch_from_rows(workspace / "runs", "init-a") == (0, [], {
        "initiative": "init-a",
        "repo": "/r/a",
        "phases": [{"id": "p1", "goal": "g1"}, {"id": "p2", "goal": "g2"}],
        "tasks": [
            {"id": "t1", "phase": "p1", "state": "ready", "needs": [], "surfaces": ["a.py"], "body": "do one"},
            {"id": "t2", "phase": "p2", "state": "todo", "needs": ["t1"], "surfaces": ["b.py", "c.py"], "body": "do two"},
        ],
        "ready": ["t1"],
    })
    assert not (workspace / "work").exists() and not (workspace / "intake").exists()


def test_repo_flag_wins_over_the_rows(workspace, monkeypatch):
    _store(monkeypatch, [HEAD, T1, STAMPED])
    assert route.launch_from_rows(workspace / "runs", "init-a", "/r/b")[2]["repo"] == "/r/b"


def test_intake_matched_by_the_initiative_its_stamp_records():
    assert route.initiative_repo({**HEAD, "extra": {"phases": []}}, [STAMPED]) == "/r/a"


def test_task_whose_needs_are_not_done_is_not_ready():
    blocked = _task("t3", "p1", "ready", ["t2"])
    assert [r["task_id"] for r in route.ready_tasks([T1, T2, blocked])] == ["t1"]


def test_task_whose_needs_are_done_is_ready():
    assert [r["task_id"] for r in route.ready_tasks([{**T1, "state": "done"}, {**T2, "state": "ready"}])] == ["t2"]


def test_missing_initiative_row_reports_and_starts_nothing(workspace, monkeypatch):
    _store(monkeypatch, [T1, STAMPED])
    assert route.launch_from_rows(workspace / "runs", "init-a") == (
        2, ["routing: no initiative row init-a in the store, or the store is unavailable"], None,
    )


def test_no_repo_in_rows_and_no_flag_reports_and_starts_nothing(workspace, monkeypatch):
    _store(monkeypatch, [HEAD, T1, {**INTAKE, "extra": {}}])
    assert route.launch_from_rows(workspace / "runs", "init-a") == (
        2, ["routing: no --repo given and no repo in the rows for init-a or its intake"], None,
    )
