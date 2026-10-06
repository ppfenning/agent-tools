from pathlib import Path

import pytest

from agent_tools import route, run_store

INTAKE = {
    "kind": "intake", "initiative": "", "task_id": "I1", "phase": "", "state": "queued",
    "needs": [], "title": "Idea", "surfaces": [], "body": "the idea", "extra": {"id": "I1"},
}
PLAN = {
    "id": "init-a", "title": "A", "body": "about A", "phases": [{"id": "p1", "goal": "g"}],
    "tasks": [
        {"id": "t1", "phase": "p1", "title": "T1", "body": "x", "needs": [], "surfaces": ["a.py"]},
        {"id": "t2", "phase": "p1", "title": "T2", "body": "y", "needs": ["t1"]},
    ],
}


class Store:
    """A recording stand-in for the two run_store calls the edge makes."""

    def __init__(self, rows):
        self.rows = {(r["kind"], r["task_id"]): r for r in rows}
        self.upserts = []

    def read_queue(self, runs_dir, initiative=None, kind=None):
        return [
            r for r in self.rows.values()
            if (initiative is None or r["initiative"] == initiative) and (kind is None or r["kind"] == kind)
        ]

    def upsert_row_detail(self, runs_dir, row):
        self.upserts.append(row)
        self.rows[(row["kind"], row["task_id"])] = row
        return ""


@pytest.fixture
def store(monkeypatch, tmp_path):
    fake = Store([INTAKE])
    monkeypatch.setattr(run_store, "read_queue", fake.read_queue)
    monkeypatch.setattr(run_store, "upsert_row_detail", fake.upsert_row_detail)
    monkeypatch.chdir(tmp_path)
    return fake


def _summary(rows):
    return [(r["kind"], r["task_id"], r["state"]) for r in rows]


def test_intake_row_in_plan_rows_out(store, tmp_path):
    seen = []
    code, lines = route.decompose_into_rows(Path("runs"), "I1", lambda data: seen.append(data) or PLAN)
    assert (code, lines) == (0, ["routing: wrote 4 rows for init-a"])
    assert seen == [{"id": "I1", "body": "the idea", "state": "queued"}]
    assert _summary(store.upserts) == [
        ("initiative", "init-a", "todo"), ("task", "t1", "ready"), ("task", "t2", "todo"), ("intake", "I1", "done"),
    ]
    assert store.upserts[-1]["extra"] == {"id": "I1", "initiative": "init-a"}
    assert not (tmp_path / "work").exists() and not (tmp_path / "intake").exists()


def test_second_run_repeats_the_upserts(store):
    route.decompose_into_rows(Path("runs"), "I1", lambda data: PLAN)
    first = list(store.upserts)
    route.decompose_into_rows(Path("runs"), "I1", lambda data: PLAN)
    assert store.upserts[len(first):] == first


def test_ticket_past_ready_is_not_reset(store):
    route.decompose_into_rows(Path("runs"), "I1", lambda data: PLAN)
    store.rows[("task", "t1")] = {**store.rows[("task", "t1")], "state": "in_progress"}
    store.upserts.clear()
    route.decompose_into_rows(Path("runs"), "I1", lambda data: PLAN)
    assert _summary(store.upserts) == [
        ("initiative", "init-a", "todo"), ("task", "t2", "todo"), ("intake", "I1", "done"),
    ]
    assert store.rows[("task", "t1")]["state"] == "in_progress"


def test_zero_task_plan_writes_nothing(store):
    empty = {**PLAN, "tasks": [], "lint": ["no tasks found"]}
    assert route.decompose_into_rows(Path("runs"), "I1", lambda data: empty) == (1, ["routing: no tasks found"])
    assert store.upserts == []


def test_missing_intake_row_runs_nothing(store):
    ran = []
    assert route.decompose_into_rows(Path("runs"), "I9", lambda data: ran.append(data) or PLAN)[0] == 2
    assert ran == [] and store.upserts == []
