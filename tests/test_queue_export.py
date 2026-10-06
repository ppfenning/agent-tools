from pathlib import Path

from agent_tools.queue_export import export_files, export_rows, is_contentless, plan_export, skipped_ids
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
EMPTY_ROW = {
    "kind": None,
    "initiative": None,
    "task_id": "ghost",
    "phase": None,
    "state": None,
    "needs": None,
    "title": None,
    "surfaces": None,
    "body": None,
    "extra": None,
}


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

    first, skipped = export_files(tmp_path, rows)
    assert skipped == []
    assert first == sorted([row_path(TASK_ROW), row_path(INTAKE_ROW)])
    assert (tmp_path / row_path(TASK_ROW)).read_text() == render_item(TASK_ROW)
    assert (tmp_path / row_path(INTAKE_ROW)).read_text() == render_item(INTAKE_ROW)
    assert (tmp_path / "work" / "queue" / "initiative.md").read_text() == "# Queue\n"

    assert export_files(tmp_path, rows) == ([], [])


def test_export_files_empty_rows_is_noop(tmp_path: Path) -> None:
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "stale.md").write_text("---\ntitle: Stale\n---\n")

    assert export_files(tmp_path, []) == ([], [])
    assert (tmp_path / "intake" / "stale.md").exists()


def test_is_contentless() -> None:
    assert is_contentless(EMPTY_ROW)
    assert is_contentless({**TASK_ROW, "kind": None})
    assert not any(is_contentless(row) for row in (TASK_ROW, INTAKE_ROW, INITIATIVE_ROW))


def test_skipped_ids_names_contentless_rows() -> None:
    assert skipped_ids([TASK_ROW, EMPTY_ROW]) == ["ghost"]
    assert skipped_ids([{**EMPTY_ROW, "task_id": None}, TASK_ROW, {**EMPTY_ROW, "task_id": None}]) == ["#0", "#2"]


def test_export_rows_fills_partly_empty_row() -> None:
    row = {**TASK_ROW, "extra": None, "body": None, "needs": None}
    assert export_rows([row]) == {row_path(TASK_ROW): render_item({**TASK_ROW, "body": "", "needs": []})}


def test_export_files_only_contentless_rows_changes_nothing(tmp_path: Path) -> None:
    (tmp_path / "intake").mkdir()
    (tmp_path / "intake" / "stray.md").write_text("---\ntitle: T\n---\n")
    assert export_files(tmp_path, [EMPTY_ROW]) == ([], ["ghost"])
    assert (tmp_path / "intake" / "stray.md").exists()


def test_export_files_keeps_file_named_by_skipped_row(tmp_path: Path) -> None:
    hollow = {**TASK_ROW, "title": None, "body": None, "extra": None}
    (tmp_path / row_path(TASK_ROW)).parent.mkdir(parents=True)
    (tmp_path / row_path(TASK_ROW)).write_text("last copy\n")
    touched, skipped = export_files(tmp_path, [INTAKE_ROW, hollow])
    assert skipped == ["codec"]
    assert touched == [row_path(INTAKE_ROW)]
    assert (tmp_path / row_path(TASK_ROW)).read_text() == "last copy\n"


def test_export_rows_skips_contentless_row() -> None:
    assert list(export_rows([TASK_ROW, EMPTY_ROW])) == [row_path(TASK_ROW)]


def test_plan_export_spares_held_and_done() -> None:
    files_on_disk = {"intake/held/a.md": "x", "intake/done/b.md": "y", "intake/stray.md": "z"}
    _, deletes = plan_export([TASK_ROW], files_on_disk)
    assert deletes == ["intake/stray.md"]


def test_export_files_skips_contentless_row(tmp_path: Path) -> None:
    touched, skipped = export_files(tmp_path, [TASK_ROW, EMPTY_ROW])
    assert skipped == ["ghost"]
    assert touched == [row_path(TASK_ROW)]
    assert (tmp_path / row_path(TASK_ROW)).read_text() == render_item(TASK_ROW)


def test_export_files_keeps_held_and_done_deletes_stray(tmp_path: Path) -> None:
    for name in ("held/h.md", "done/d.md", "stray.md"):
        path = tmp_path / "intake" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ntitle: T\n---\n")

    touched, _ = export_files(tmp_path, [TASK_ROW])
    assert (tmp_path / "intake" / "held" / "h.md").exists()
    assert (tmp_path / "intake" / "done" / "d.md").exists()
    assert not (tmp_path / "intake" / "stray.md").exists()
    assert "intake/stray.md" in touched
