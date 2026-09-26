from pathlib import Path

from agent_tools.chair_read_approved import approved_rows, read_approved


def _item(id: str, state: str, phase: str = "build", needs: list[str] | None = None) -> dict:
    return {"id": id, "initiative": "init", "phase": phase, "state": state, "repo": "r", "needs": needs or []}


def test_an_approved_item_yields_one_row_with_the_five_keys() -> None:
    assert approved_rows([_item("a", "approved")]) == [
        {"id": "a", "initiative": "init", "repo": "r", "phase": "build", "phase_done": True, "needs": [], "run": ""}
    ]


def test_a_draft_item_is_skipped() -> None:
    assert approved_rows([_item("a", "todo"), _item("b", "ready")]) == []


def test_phase_done_is_false_while_a_sibling_is_not_done() -> None:
    rows = approved_rows([_item("a", "approved"), _item("b", "ready")])
    assert [r["phase_done"] for r in rows] == [False]


def test_phase_done_is_true_once_all_siblings_are_done() -> None:
    rows = approved_rows([_item("a", "approved"), _item("b", "done"), _item("c", "ready", phase="next")])
    assert [r["phase_done"] for r in rows] == [True]


def test_needs_is_carried_through_as_a_list() -> None:
    needs = ["x", "y"]
    rows = approved_rows([_item("a", "approved", needs=needs)])
    assert rows[0]["needs"] == ["x", "y"]
    assert rows[0]["needs"] is not needs


def test_the_edge_reads_the_work_store(tmp_path: Path) -> None:
    init = tmp_path / "work" / "init"
    (init / "build").mkdir(parents=True)
    (init / "initiative.md").write_text("---\nid: init\nrepo: /r/x\n---\nbody\n", encoding="utf-8")
    (init / "build" / "a.md").write_text("---\nid: a\nstate: approved\nneeds: [z]\n---\nbody\n", encoding="utf-8")
    (init / "build" / "b.md").write_text("---\nid: b\nstate: done\n---\nbody\n", encoding="utf-8")
    assert read_approved(tmp_path) == [
        {"id": "a", "initiative": "init", "repo": "/r/x", "phase": "build", "phase_done": True, "needs": ["z"], "run": ""}
    ]
