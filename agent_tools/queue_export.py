"""Pure export of queue rows to the intake/ and work/ trees, plus the edge that applies it.

`plan_export` takes the rows `run_store.read_queue` returned and the text currently on disk, and
says which files to write and which to delete to make the tree match the rows. It never touches a
filesystem; `export_files` is the edge that reads, plans, and writes. `initiative.md` files are
never part of a row and are never written or deleted, whatever `files_on_disk` holds for them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_tools.queue_rows import render_item, row_path

Row = dict[str, Any]

_TREES = ("intake", "work")


def plan_export(rows: list[Row], files_on_disk: dict[str, str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(writes, deletes) to bring `files_on_disk` to match `rows`. `render_item` is deterministic,
    so a path whose text already matches is left alone, and a board that changed nothing gives ([], [])."""
    target = {row_path(row): render_item(row) for row in rows}
    writes = [(path, text) for path, text in target.items() if files_on_disk.get(path) != text]
    deletes = [path for path in files_on_disk if path not in target and Path(path).name != "initiative.md"]
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


def export_files(workspace: Path, rows: list[Row]) -> list[str]:
    """Edge: write and delete files under `workspace` so intake/ and work/ match `rows`, and return
    the sorted list of paths touched. An empty `rows` means the store was unavailable, not an empty
    board, so it changes nothing and returns []."""
    if not rows:
        return []
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
    return sorted({path for path, _ in writes} | set(deletes))
