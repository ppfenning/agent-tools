"""`cox route store-fill`: fill the store's work_items from the workspace files.

The plan is pure: `plan_fill` takes the rows `queue_rows.parse_item` built and the rows the store holds, and returns
the writes. Reading files and calling `run_store` is the edge's job, at the bottom of this module.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from agent_tools import queue_rows, run_store

Row = queue_rows.Row

# The fields a file can supply to a row that lacks them. state, needs and priority are never here: the store owns them.
_FILLABLE = ("kind", "phase", "title", "surfaces", "body", "extra")
_CLAIM_KEYS = ("holder", "epoch", "expires_at")  # store-only columns; a write never carries them
_UNREADABLE = "store-fill: the store could not be read; nothing written"


@dataclass(frozen=True)
class Plan:
    writes: tuple[Row, ...]
    inserted: int
    filled: int
    unchanged: int


def _key(row: Row) -> tuple[str, str]:
    return row["initiative"], row["task_id"]


def _filled(parsed: Row, stored: Row) -> Row | None:
    """The stored row with each missing (None) field taken from `parsed`; None when nothing is missing.
    An empty parsed value is not taken: a store that reads "" or {} back as None would refill it on every run."""
    gaps = {field: parsed[field] for field in _FILLABLE if stored.get(field) is None and parsed.get(field)}
    if not gaps:
        return None
    return {**{k: v for k, v in stored.items() if k not in _CLAIM_KEYS}, **gaps}


def plan_fill(parsed: list[Row], existing: list[Row]) -> Plan:
    """Insert a row the store lacks; fill only the missing content of one it has. First row per key wins, so
    `intake/x.md` and `intake/done/x.md` cannot overwrite each other."""
    first_at = {_key(row): i for i, row in reversed(list(enumerate(parsed)))}
    deduped = [row for i, row in enumerate(parsed) if first_at[_key(row)] == i]
    stored = {_key(row): row for row in existing}
    outcomes = [
        ("inserted", row) if _key(row) not in stored else ("filled", _filled(row, stored[_key(row)]))
        for row in deduped
    ]
    writes = tuple(write for tag, write in outcomes if write is not None)
    inserted = sum(tag == "inserted" for tag, _ in outcomes)
    return Plan(writes, inserted, len(writes) - inserted, len(outcomes) - len(writes))


def shape_of(parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    """(`kind`, `path_parts`) for parse_item from a workspace-relative path, or None outside work/ and intake/."""
    if len(parts) == 3 and parts[0] == "work" and parts[2] == "initiative.md":
        return "initiative", parts[1:]
    if len(parts) == 4 and parts[0] == "work":
        return "task", parts[1:]
    if len(parts) in (2, 3) and parts[0] == "intake":
        return "intake", parts[1:]
    return None


def parse_workspace(files: list[tuple[tuple[str, ...], str]]) -> tuple[list[Row], int]:
    """The rows parse_item builds from `(path_parts, text)` pairs, and how many files gave none.
    parse_item has no shape for intake/held/, so those files land in the skipped count."""
    rows = [
        None if (shape := shape_of(parts)) is None else queue_rows.parse_item(*shape, text) for parts, text in files
    ]
    return [row for row in rows if row is not None], sum(row is None for row in rows)


def format_summary(plan: Plan, skipped: int) -> str:
    return f"inserted {plan.inserted}, filled {plan.filled}, unchanged {plan.unchanged}, skipped {skipped}"


def _read_store(runs_dir: Path) -> list[Row] | None:
    """Edge. None when the store cannot be opened or read; [] only for a store that opened and holds no rows.
    `read_queue` returns [] for both, so the store is opened and counted first, and rows missing from a
    counted store mean the read failed."""
    try:
        conn = run_store.connect_readonly(runs_dir)
        if conn is None:
            return None
        try:
            held = conn.execute("SELECT COUNT(*) AS n FROM work_items").fetchone()["n"]
        finally:
            conn.close()
    except Exception:  # an absent table, an unreachable Postgres or a missing driver all mean unreadable
        return None
    rows = run_store.read_queue(runs_dir)
    return None if held and not rows else rows


def _workspace_files(ws: Path) -> list[tuple[tuple[str, ...], str]]:
    """Edge. `(path_parts, text)` for every initiative.md, ticket and intake file, held and done included.
    A file that cannot be read as text is left out, so it counts as skipped."""
    paths = sorted(
        p
        for pattern in ("work/*/initiative.md", "work/*/*/*.md", "intake/*.md", "intake/done/*.md", "intake/held/*.md")
        for p in ws.glob(pattern)
    )
    texts = [(p.relative_to(ws).parts, _text_or_none(p)) for p in paths]
    return [(parts, text) for parts, text in texts if text is not None]


def _text_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def main(a: argparse.Namespace) -> int:
    """Exits 2 without writing when the store cannot be read, and the moment a row fails to upsert."""
    from agent_tools import cli  # lazy: cli's table names this module as a handler

    ws = cli._route_import_workspace(a)
    if isinstance(ws, str):
        print(f"store-fill: {ws}")
        return 2
    runs_dir = ws / "runs"
    existing = _read_store(runs_dir)
    if existing is None:
        print(_UNREADABLE)
        return 2
    if not (ws / "work").is_dir() and not (ws / "intake").is_dir():
        print(f"store-fill: no work/ or intake/ under {ws}; pass --workspace or set workspace_dir in the profile")
        return 2
    files = _workspace_files(ws)
    parsed, skipped = parse_workspace(files)
    plan = plan_fill(parsed, existing)
    for written, row in enumerate(plan.writes):
        detail = run_store.upsert_row_detail(runs_dir, row)
        if detail:
            print(f"store-fill: store refused {row['initiative']}/{row['task_id']}: {detail}")
            print(f"store-fill: wrote {written} of {len(plan.writes)} rows before the refusal")
            return 2
    print(format_summary(plan, skipped))
    return 0
