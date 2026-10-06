from datetime import UTC, datetime
from pathlib import Path

from agent_tools import chair_read_idle_backlog as mod
from agent_tools.chair_read_idle_backlog import (
    blocked_ready,
    empty_stubs,
    free_lanes,
    idle_backlog,
    last_progress_at,
    read_idle_backlog,
    stub_candidates,
)

NOW = datetime(2026, 10, 5, tzinfo=UTC)
EMPTY = {"free_lanes": 0, "ready": 0, "queued": 0, "last_progress_at": None, "empty_stubs": [], "blocked_ready": []}


def item(task: str, state: str, needs: list[str] | None = None, initiative: str = "a") -> dict:
    return {"id": task, "initiative": initiative, "phase": "p1", "state": state, "needs": needs or []}


def docket(ready: int, busy: int = 0, lanes: int = 4) -> dict:
    tasks = [{"id": f"t{n}", "needs": [], "requires": []} for n in range(ready)]
    return {"initiatives": [{"id": "a", "ready_tasks": tasks}], "busy_lanes": busy, "max_in_flight": lanes}


def test_four_lanes_with_three_live_runs_leaves_one_free():
    assert free_lanes(4, 3) == 1


def test_more_live_runs_than_lanes_leaves_zero_free():
    assert free_lanes(2, 5) == 0


def test_ready_and_queued_are_counted_separately():
    plain = {
        "docket": docket(ready=2),
        "items": [],
        "queued": ["intake/x.md", "intake/y.md", "intake/z.md"],
        "last_launch": None,
        "last_land": None,
        "node_calls": [],
        "with_file": [],
        "decomposed": {},
        "live": [],
    }
    got = idle_backlog(plain)
    assert (got["ready"], got["queued"]) == (2, 3)


def test_last_progress_is_the_newest_of_the_three_timestamps():
    got = last_progress_at("2026-10-05T01:00:00Z", "2026-10-05T03:00:00+00:00", ["2026-10-05T02:00:00Z"])
    assert got == "2026-10-05T03:00:00Z"


def test_last_progress_is_none_with_no_history():
    assert last_progress_at(None, None, []) is None


def test_an_initiative_with_a_file_and_no_tasks_is_a_stub():
    assert empty_stubs(["a"], [], {}, []) == ["a"]


def test_an_initiative_with_a_task_is_not_a_stub():
    assert empty_stubs(["a"], [item("t1", "ready")], {}, []) == []


def test_a_stub_with_a_live_decompose_is_not_a_stub():
    assert empty_stubs(["a"], [], {"a": "a-1"}, ["a"]) == []


def test_a_draft_initiative_is_not_a_stub_candidate():
    assert stub_candidates({"a": "---\nid: a\n---\n", "d": "---\nid: d\ndraft: true\n---\n"}) == ["a"]


def test_a_task_needing_only_an_approved_unlanded_task_is_blocked_ready():
    items = [item("t1", "approved"), item("t2", "todo", ["t1"])]
    assert blocked_ready(items) == [{"task": "t2", "unlanded_needs": ["t1"]}]


def test_the_same_task_also_needing_an_unapproved_task_is_not_blocked_ready():
    items = [item("t1", "approved"), item("t3", "todo"), item("t2", "todo", ["t1", "t3"])]
    assert blocked_ready(items) == []


def _stub_workspace(ws: Path) -> None:
    for name, extra in (("stub", ""), ("live-stub", ""), ("drafted", "draft: true\n")):
        (ws / "work" / name).mkdir(parents=True)
        (ws / "work" / name / "initiative.md").write_text(f"---\nid: {name}\n{extra}---\n")


def test_the_edge_probes_stubs_for_a_live_run(tmp_path: Path, monkeypatch):
    _stub_workspace(tmp_path)
    monkeypatch.setattr(mod, "read_live_initiatives", lambda _runs, names, _now: [n for n in names if n == "live-stub"])
    monkeypatch.setattr(mod, "read_docket", lambda *_a, **_k: docket(ready=0))
    assert read_idle_backlog(tmp_path, "files", 4, NOW)["empty_stubs"] == ["stub"]


def test_an_unreadable_docket_frees_no_lanes_even_with_ready_items(tmp_path: Path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(mod, "read_docket", boom)
    monkeypatch.setattr(mod, "read_work_items", lambda *_a: [(Path("x"), "", item("t1", "ready"))])
    assert read_idle_backlog(tmp_path, "files", 4, NOW)["free_lanes"] == 0


def test_the_edge_never_raises_and_reads_an_unreadable_workspace_as_empty(tmp_path: Path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("unreadable")

    for name in (
        "read_work_items",
        "read_docket",
        "read_intake",
        "read_chair_actions",
        "local_runs",
        "read_live_initiatives",
        "_initiative_texts",
    ):
        monkeypatch.setattr(mod, name, boom)
    assert read_idle_backlog(tmp_path / "missing", "files", 4, NOW) == EMPTY
