"""Pure export of queue rows to the intake/ and work/ trees, plus the edge that applies it.

`plan_export` takes the rows `run_store.read_queue` returned and the text currently on disk, and
says which files to write and which to delete to make the tree match the rows. It never touches a
filesystem; `export_files` is the edge that reads, plans, and writes. An `initiative.md` is written
when an initiative row holds it, and is never deleted, whatever `files_on_disk` holds for it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from agent_tools.queue_rows import render_item, row_path

Row = dict[str, Any]

_TREES = ("intake", "work")
_KEEP_PREFIXES = ("intake/held/", "intake/done/")  # never deleted, whether or not a row names them
_PATH_KEYS = {"task": ("initiative", "phase", "task_id"), "initiative": ("initiative",), "intake": ("task_id",)}


class Export(NamedTuple):
    touched: list[str]
    skipped: list[str]


def is_contentless(row: Row) -> bool:
    """True for a row with nothing to render: no kind, or title, body and extra all None."""
    return row.get("kind") is None or all(row.get(key) is None for key in ("title", "body", "extra"))


def skipped_ids(rows: list[Row]) -> list[str]:
    """The task_id of each contentless row, in row order; `#<index in rows>` when it has none, so each stays distinct."""
    return [str(row.get("task_id") or f"#{i}") for i, row in enumerate(rows) if is_contentless(row)]


def _named_path(row: Row) -> str | None:
    """row_path when the kind and the fields it keys on are set, else None: a contentless row may still name its file."""
    keys = _PATH_KEYS.get(row.get("kind"))
    named = keys is not None and all(row.get(key) is not None for key in keys)
    return row_path({"state": None, **row}) if named else None


def _filled(row: Row) -> Row:
    """None in a content field becomes its empty value, so render_item never sees None where it expects text or a collection."""
    empty = {"title": "", "needs": [], "surfaces": [], "body": "", "extra": {}}
    return {**row, **{key: row.get(key) or value for key, value in empty.items()}}


def export_rows(rows: list[Row]) -> dict[str, str]:
    """Workspace-relative path to file text for every row with content: initiative, tickets and intake alike."""
    return {row_path(row): render_item(_filled(row)) for row in rows if not is_contentless(row)}


def plan_export(rows: list[Row], files_on_disk: dict[str, str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(writes, deletes) to bring `files_on_disk` to match `rows`. `render_item` is deterministic,
    so a path whose text already matches is left alone, and a board that changed nothing gives ([], []).
    A contentless row is not written, but the path it names is kept, never deleted."""
    target = export_rows(rows)
    kept = {_named_path(row) for row in rows if is_contentless(row)}
    writes = [(path, text) for path, text in target.items() if files_on_disk.get(path) != text]
    deletes = [
        path
        for path in files_on_disk
        if path not in target
        and path not in kept
        and Path(path).name != "initiative.md"
        and not path.startswith(_KEEP_PREFIXES)
    ]
    return writes, deletes


def _read_files(workspace: Path) -> dict[str, str]:
    roots = [workspace / tree for tree in _TREES if (workspace / tree).is_dir()]
    return {
        path.relative_to(workspace).as_posix(): path.read_text()
        for root in roots
        for path in root.rglob("*.md")
    }


def _prune_empty_dirs(directory: Path, stop_at: set[Path]) -> None:
    while directory not in stop_at and directory.is_dir() and not any(directory.iterdir()):
        parent = directory.parent
        directory.rmdir()
        directory = parent


def export_files(workspace: Path, rows: list[Row]) -> Export:
    """Edge: write and delete files under `workspace` so intake/ and work/ match `rows`.
    Returns the sorted paths touched and the ids of contentless rows skipped. Nothing under
    intake/held/ or intake/done/ is deleted. Rows with no content to export mean the store was
    unavailable or degraded, not an empty board, so the tree is left unchanged."""
    skipped = skipped_ids(rows)
    if len(skipped) == len(rows):
        return Export([], skipped)
    workspace = Path(workspace)
    writes, deletes = plan_export(rows, _read_files(workspace))
    stop_at = {workspace, workspace / "intake", workspace / "work"}
    for path, text in writes:
        full = workspace / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)
    for path in deletes:
        full = workspace / path
        full.unlink()
        _prune_empty_dirs(full.parent, stop_at)
    return Export(sorted({path for path, _ in writes} | set(deletes)), skipped)
