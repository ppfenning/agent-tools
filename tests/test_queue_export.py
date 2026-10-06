from pathlib import Path

from agent_tools.queue_export import export_files, export_rows, plan_export
from agent_tools.queue_rows import render_item, row_path

TASK_ROW = {
    "kind": "task",
    "initiative": "queue",
    "task_id": "codec",
    "phase": "q",
    "state": "doing",
    "needs": ["a-1"],
    "title": "Codec: rows",
    "surfaces": ["agent_tools/x.py"],
    "body": "Body line.\n",
    "extra": {},
}
INTAKE_ROW = {
    "kind": "intake",
    "initiative": "intake",
    "task_id": "idea",
    "phase": "",
    "state": "queued",
    "needs": [],
    "title": "Idea",
    "surfaces": [],
    "body": "Some idea.\n",
    "extra": {},
}
DONE_ROW = {**INTAKE_ROW, "state": "done"}
INITIATIVE_ROW = {
    "kind": "initiative",
    "initiative": "queue",
    "task_id": "queue",
    "phase": "",
    "state": "todo",
    "needs": [],
    "title": "Queue",
    "surfaces": [],
    "body": "Why.\n",
    "extra": {
        "id": "queue",
        "intake": "intake/idea.md",
        "phases": [{"id": "q", "goal": "Build it"}],
    },
}
STAMPED_ROW = {**INTAKE_ROW, "state": "done", "extra": {"initiative": "queue"}}


def test_export_rows_initiative() -> None:
    assert export_rows([INITIATIVE_ROW]) == {
        "work/queue/initiative.md": (
            "---\ntitle: Queue\nid: queue\nintake: intake/idea.md\nphases:\n- id: q\n  goal: Build it\n---\nWhy.\n"
        )
    }


def test_export_rows_task_with_needs_and_surfaces() -> None:
    assert export_rows([TASK_ROW]) == {
        "work/queue/q/codec.md": (
            "---\nstate: doing\ntitle: 'Codec: rows'\nneeds:\n- a-1\nsurfaces:\n- agent_tools/x.py\n---\nBody line.\n"
        )
    }


def test_export_rows_stamped_intake() -> None:
    assert export_rows([STAMPED_ROW]) == {
        "intake/done/idea.md": "---\ntitle: Idea\ninitiative: queue\n---\nSome idea.\n"
    }


def test_export_rows_is_deterministic() -> None:
    rows = [INITIATIVE_ROW, TASK_ROW, STAMPED_ROW]
    before = [dict(row) for row in rows]
    assert export_rows(rows) == export_rows(rows)
    assert rows == before


def test_plan_export_new_file() -> None:
    writes, deletes = plan_export([TASK_ROW], {})
    assert writes == [(row_path(TASK_ROW), render_item(TASK_ROW))]
    assert deletes == []


def test_plan_export_changed_file() -> None:
    path = row_path(TASK_ROW)
    writes, deletes = plan_export([TASK_ROW], {path: "stale text"})
    assert writes == [(path, render_item(TASK_ROW))]
    assert deletes == []


def test_plan_export_unchanged_is_empty() -> None:
    path = row_path(TASK_ROW)
    writes, deletes = plan_export([TASK_ROW], {path: render_item(TASK_ROW)})
    assert (writes, deletes) == ([], [])


def test_plan_export_moved_intake_writes_and_deletes() -> None:
    old_path = row_path(INTAKE_ROW)
    new_path = row_path(DONE_ROW)
    writes, deletes = plan_export([DONE_ROW], {old_path: render_item(INTAKE_ROW)})
    assert writes == [(new_path, render_item(DONE_ROW))]
    assert deletes == [old_path]


def test_plan_export_initiative_md_untouched() -> None:
    files_on_disk = {"work/queue/initiative.md": "# Queue\n"}
    writes, deletes = plan_export([], files_on_disk)
    assert (writes, deletes) == ([], [])


def test_plan_export_empty_rows_is_empty() -> None:
    assert plan_export([], {}) == ([], [])


def test_export_files_second_export_is_noop(tmp_path: Path) -> None:
    rows = [TASK_ROW, INTAKE_ROW]
    (tmp_path / "work" / "queue").mkdir(parents=True)
    (tmp_path / "work" / "queue" / "initiative.md").write_text("# Queue\n")

    first = export_files(tmp_path, rows)
    assert first == sorted([row_path(TASK_ROW), row_path(INTAKE_ROW)])
    assert (tmp_path / row_path(TASK_ROW)).read_text() == render_item(TASK_ROW)
    assert (tmp_path / row_path(INTAKE_ROW)).read_text() == render_item(INTAKE_ROW)
    assert (tmp_path / "work" / "queue" / "initiative.md").read_text() == "# Queue\n"

    second = export_files(tmp_path, rows)
    assert second == []


def test_export_files_empty_rows_is_noop(tmp_path: Path) -> None:
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "stale.md").write_text("---\ntitle: Stale\n---\n")

    assert export_files(tmp_path, []) == []
    assert (tmp_path / "intake" / "stale.md").exists()
