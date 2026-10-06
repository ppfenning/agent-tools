"""Register graphs' per-run trace Parquet files into the lake traces table with add_files.

The files stay where graphs wrote them, at <root>/YYYY/MM/DD/<run_id>.parquet. Nothing is copied.
A file counts as registered when its path is among the traces table's current data files, so a rerun adds nothing.
Local roots are bare paths: a `file://` prefix is stripped from the root and from the registered paths before comparing."""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterable

from pyiceberg.catalog import Catalog
from pyiceberg.table import Table

from agent_tools.lake_tables import NAMESPACE

# As in lake_sync: a missing pyarrow fails this import, outside the sync lease, without loading pyarrow.
if importlib.util.find_spec("pyarrow") is None:
    raise ModuleNotFoundError("No module named 'pyarrow'", name="pyarrow")

__all__ = ["new_files", "register_traces", "trace_files"]

_LAYOUT = re.compile(r"\d{4}/\d{2}/\d{2}/[^/]+\.parquet")


def _bare(path: str) -> str:
    """A local `file://` URI as a bare path; any other path unchanged."""
    return path.removeprefix("file://")


def trace_files(paths: Iterable[str], root: str) -> list[str]:
    """Paths of the form <root>/YYYY/MM/DD/<run_id>.parquet, sorted."""
    prefix = root.rstrip("/") + "/"
    return sorted(p for p in paths if p.startswith(prefix) and _LAYOUT.fullmatch(p[len(prefix):]))


def new_files(found: Iterable[str], registered: Iterable[str]) -> list[str]:
    """The found paths not yet registered, in the order found."""
    known = frozenset(registered)
    return [p for p in found if p not in known]


def _list_files(table: Table, root: str) -> list[str]:
    """Edge. Every file under root through the table's FileIO, spelled as root is; [] when root is absent."""
    from pyarrow.fs import FileSelector, FileType
    from pyiceberg.io.pyarrow import PyArrowFileIO  # imports pyarrow itself, so it stays lazy too

    scheme, netloc, path = PyArrowFileIO.parse_location(root)
    fs = table.io.fs_by_scheme(scheme, netloc)
    infos = fs.get_file_info(FileSelector(path, recursive=True, allow_not_found=True))
    base = path.rstrip("/") + "/"
    return [
        root.rstrip("/") + "/" + info.path[len(base):]
        for info in infos
        if info.type == FileType.File and info.path.startswith(base)
    ]


def _registered(table: Table) -> set[str]:
    """Edge. Paths of the table's current data files."""
    return {_bare(task.file.file_path) for task in table.scan().plan_files()}


def register_traces(catalog: Catalog, traces_root: str, dry_run: bool = False) -> tuple[int, int, int]:
    """Edge. Return (found, registered, skipped). `found` counts layout-matching files under traces_root,
    `skipped` those already registered. A dry run registers none, so `registered` is 0 and nothing is written."""
    table = catalog.load_table(f"{NAMESPACE}.traces")
    root = _bare(traces_root)
    found = trace_files(_list_files(table, root), root)
    new = new_files(found, _registered(table))
    if new and not dry_run:
        table.add_files(file_paths=new)
    registered = 0 if dry_run else len(new)
    return len(found), registered, len(found) - len(new)
