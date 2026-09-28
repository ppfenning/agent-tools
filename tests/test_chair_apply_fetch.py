import json
from pathlib import Path

import pytest

from agent_tools.chair_apply_fetch import apply_fetched_approvals, apply_ready_to_approved, approved_task_ids


def test_a_record_that_names_its_task_under_ticket_is_approved_by_that_id():
    assert approved_task_ids([{"ticket": "t1", "review": {"verdict": "approve"}}]) == ["t1"]


def test_approved_task_ids_keeps_only_the_approved_records():
    records = [
        {"task": "t1", "review": {"verdict": "approve"}},
        {"task": "t2", "review": {"verdict": "revise"}},
        {"task": "t3"},
    ]
    assert approved_task_ids(records) == ["t1"]


def test_apply_ready_to_approved_moves_ready_to_approved():
    text = "---\nstate: ready\ntitle: x\n---\nbody\n"
    assert apply_ready_to_approved(text) == ("---\nstate: approved\ntitle: x\n---\nbody\n", None)


@pytest.mark.parametrize("state", ["approved", "done"])
def test_apply_ready_to_approved_is_a_no_op_once_already_moved(state: str):
    assert apply_ready_to_approved(f"---\nstate: {state}\n---\nbody\n") == (None, None)


def test_apply_ready_to_approved_refuses_any_other_state():
    new_text, note = apply_ready_to_approved("---\nstate: quarantined\n---\nbody\n")
    assert new_text is None
    assert note == "chair_apply_fetch: ticket state is 'quarantined', not moving to approved"


def _write_record(runs_dir: Path, run: str, phase: str, task: str, body) -> None:
    path = runs_dir / run / "tasks" / phase / f"{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")


def _write_ticket(work_dir: Path, initiative: str, phase: str, task: str, state: str) -> Path:
    path = work_dir / "work" / initiative / phase / f"{task}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstate: {state}\n---\nbody\n", encoding="utf-8")
    return path


def test_apply_fetched_approvals_moves_an_approved_ready_ticket_and_returns_its_id(tmp_path: Path):
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _write_record(runs_dir, "run-1", "p1", "t1", {"task": "t1", "review": {"verdict": "approve"}})
    ticket = _write_ticket(work_dir, "init", "p1", "t1", "ready")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == ["t1"]
    assert "state: approved" in ticket.read_text(encoding="utf-8")


def test_apply_fetched_approvals_leaves_a_revised_task_ready_and_unreturned(tmp_path: Path):
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _write_record(runs_dir, "run-1", "p1", "t1", {"task": "t1", "review": {"verdict": "revise"}})
    ticket = _write_ticket(work_dir, "init", "p1", "t1", "ready")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == []
    assert "state: ready" in ticket.read_text(encoding="utf-8")


def test_apply_fetched_approvals_is_a_no_op_on_an_already_approved_ticket(tmp_path: Path):
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _write_record(runs_dir, "run-1", "p1", "t1", {"task": "t1", "review": {"verdict": "approve"}})
    ticket = _write_ticket(work_dir, "init", "p1", "t1", "approved")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == []
    assert "state: approved" in ticket.read_text(encoding="utf-8")


def test_apply_fetched_approvals_skips_a_task_id_with_no_matching_ticket_file(tmp_path: Path):
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _write_record(runs_dir, "run-1", "p1", "t1", {"task": "t1", "review": {"verdict": "approve"}})
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == []


def test_apply_fetched_approvals_skips_a_malformed_task_record(tmp_path: Path):
    runs_dir, work_dir = tmp_path / "runs", tmp_path
    _write_record(runs_dir, "run-1", "p1", "t1", "{not json")
    _write_ticket(work_dir, "init", "p1", "t1", "ready")
    assert apply_fetched_approvals(runs_dir, work_dir, "run-1", "init") == []
