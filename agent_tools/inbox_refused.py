"""Inbox source for refused lands: a pure row-to-item core and one thin edge over the chair action log."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools.chair_remedy import remedy_for, trim_reason
from agent_tools.inbox import InboxItem, item_id

Row = Mapping[str, Any]


def _key(row: Row) -> tuple[str, str]:
    return str(row.get("run") or ""), str(row.get("task_id") or "")


def _utc(ts: object) -> datetime | None:
    """The row's `ts` as an aware UTC datetime; naive text is read as UTC, unparseable text gives None."""
    try:
        at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.astimezone(UTC) if at.tzinfo else at.replace(tzinfo=UTC)


def open_refusals(rows: Iterable[Row]) -> list[Row]:
    """Newest refused land row per (run, task) that no later land row with status landed follows; oldest first.

    Order is by `ts`, then log position. A later not_landed or second refusal does not close a refusal.
    """
    lands = [r for r in rows if r.get("kind") == "land" and all(_key(r)) and _utc(r.get("ts")) is not None]
    ordered = sorted(enumerate(lands), key=lambda p: (_utc(p[1].get("ts")), p[0]))
    newest_refused: dict[tuple[str, str], Row] = {}
    for _, row in ordered:
        if row.get("status") == "refused":
            newest_refused[_key(row)] = row
        elif row.get("status") == "landed":
            newest_refused.pop(_key(row), None)
    return list(newest_refused.values())


def _item(row: Row) -> InboxItem:
    run, task = _key(row)
    repo = str(row.get("repo") or "")
    created = _utc(row.get("ts"))
    assert created is not None  # open_refusals only passes rows with a parseable ts
    return InboxItem(
        id=item_id("refused-command", f"{run}\0{task}"),
        kind="refused-command",
        created_at=created,
        what=f"land of {task} in {run} was refused",
        evidence=trim_reason(row.get("reason")),
        accept_cmd=tuple(remedy_for("stranded", run, task, repo)["command"]),
        deny_cmd=tuple(remedy_for("stale", run, task, repo)["command"]),
    )


def refused_to_items(rows: Iterable[Row]) -> tuple[InboxItem, ...]:
    """One item per refused land row still open: the carry argv accepts it, the drop argv denies it."""
    return tuple(_item(r) for r in open_refusals(rows))


def load_refused_rows(runs_dir: Path) -> list[Row]:
    """Edge: the open refused land rows in the chair action log under runs_dir; [] when the log is missing."""
    # chair_read_record.py holds only the writer. The log's one reader is dash_feed._action_rows, so it is reused here.
    from agent_tools.dash_feed import _action_rows

    return open_refusals(_action_rows(runs_dir))
