"""Run logs past retention: which ended runs' text logs are due, where their archived copies live, and the edge that
archives each one to the lake's object store before it removes the local file (Pat, 2026-09-28: a 7-day lookback by
default, set by the routing profile's `log_retention_days`)."""
from __future__ import annotations

import gzip
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_RETENTION_DAYS = 7
# Longest first, so `x.calls.jsonl` is read as run `x`, never as a log of run `x.calls`.
SUFFIXES = (".calls.jsonl", ".log")
GZIP_MAGIC = b"\x1f\x8b"


def run_of(name: str) -> str | None:
    """Pure. The run id a run-log file name belongs to; None for any other file."""
    return next((name[: -len(suffix)] for suffix in SUFFIXES if name.endswith(suffix)), None)


def retention_days(profile: Mapping[str, Any]) -> int:
    """Pure. The profile's `log_retention_days` as a positive int; the default for a missing, invalid or non-positive value."""
    try:
        days = int(profile.get("log_retention_days"))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return days if days >= 1 else DEFAULT_RETENTION_DAYS


def due(files: Iterable[tuple[str, float]], ended: set[str], now: datetime, days: int) -> list[str]:
    """Pure. Names of run logs whose run has ended and whose last write is more than `days` days before `now`."""
    cutoff = (now - timedelta(days=days)).timestamp()
    return sorted(name for name, mtime in files if run_of(name) in ended and mtime < cutoff)


def logs_root(traces_root: str) -> str:
    """Pure. The archive root beside the traces root: `…/traces` becomes `…/logs`, any other root gains `/logs`."""
    root = traces_root.rstrip("/")
    return root[: -len("traces")] + "logs" if root.endswith("/traces") else root + "/logs"


def object_path(root: str, name: str, mtime: float) -> str:
    """Pure. `<root>/<yyyy>/<mm>/<name>.gz`, dated by the file's last write in UTC."""
    stamp = datetime.fromtimestamp(mtime, UTC)
    return f"{root}/{stamp:%Y}/{stamp:%m}/{name}.gz"


@dataclass(frozen=True)
class Outcome:
    due: int
    archived: int
    failed: list[str] = field(default_factory=list)


def archive_and_prune(runs_dir: Path, root: str, io: Any, ended: set[str], now: datetime, days: int,
                      dry_run: bool = False) -> Outcome:
    """Edge. Write each due log to its `.gz` object path through `io` (a pyiceberg FileIO, which gzips by the name),
    read it back, and only then delete the local file. A failure keeps the local file and is reported; it never stops
    the others."""
    if not runs_dir.is_dir():
        return Outcome(0, 0)
    files = {p.name: p.stat().st_mtime for p in runs_dir.iterdir() if p.is_file() and run_of(p.name) is not None}
    names = due(files.items(), ended, now, days)
    if dry_run:
        return Outcome(len(names), 0)
    archived, failed = 0, []
    for name in names:
        path = runs_dir / name
        try:
            data = path.read_bytes()
            target = object_path(root, name, files[name])
            # pyarrow gzips an output stream by its `.gz` name but hands the object back as stored, so the raw bytes go
            # in and must come back once any gzip layer is removed.
            with io.new_output(target).create(overwrite=True) as out:
                out.write(data)
            with io.new_input(target).open() as back:
                stored = back.read()
            if (gzip.decompress(stored) if stored[:2] == GZIP_MAGIC else stored) != data:
                raise OSError(f"the archived copy at {target} does not read back as written")
            path.unlink()
            archived += 1
        except Exception as exc:  # one file's failure must not stop the others
            failed.append(f"{name}: {type(exc).__name__}: {exc}"[:200])
    return Outcome(len(names), archived, failed)
