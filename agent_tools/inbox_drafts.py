"""Inbox source for drafts: `draft_list` rows become `InboxItem` values of kind `draft`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_tools import draft_list
from agent_tools.inbox import InboxItem, item_id

# `cox route decline` requires --reason; this is the reason the inbox records.
DECLINE_REASON = "declined from inbox"
# An undated draft is kept, not dropped: the epoch sorts it first, so it leads the inbox.
UNDATED = datetime(1970, 1, 1, tzinfo=UTC)


def _created_at(record: Mapping) -> datetime | None:
    """`now` minus `age_seconds`; draft_list floors a future `proposed_at` to `now` and drops sub-seconds."""
    age, now = record.get("age_seconds"), record.get("now")
    if not isinstance(age, int) or not isinstance(now, str):
        return None
    try:
        parsed = datetime.fromisoformat(now)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC) - timedelta(seconds=age)


def _item(record: Mapping, created_at: datetime | None) -> InboxItem:
    # `id` is the frontmatter id or the directory name; the approve and decline verbs resolve a directory name.
    draft_id, by = str(record["id"]), str(record.get("proposed_by") or "unknown")
    return InboxItem(
        id=item_id("draft", draft_id),
        kind="draft",
        created_at=UNDATED if created_at is None else created_at,
        what=f"draft {draft_id} awaits approve or decline",
        evidence=f"proposed by {by}"
        if created_at is not None
        else f"proposed by {by}; age unknown: proposed_at missing or unparseable",
        accept_cmd=("cox", "route", "approve", draft_id),
        deny_cmd=("cox", "route", "decline", draft_id, "--reason", DECLINE_REASON),
    )


def drafts_to_items(records: Sequence[Mapping]) -> tuple[InboxItem, ...]:
    """One item per record, in input order; a record is a `DraftRow` as a dict plus the `now` it was listed at."""
    return tuple(_item(r, _created_at(r)) for r in records)


def load_draft_records(work_dir: Path, now: str) -> list[dict]:
    """Edge. Every row of `draft_list.read_drafts`, with the `now` its age was measured against."""
    return [{**asdict(row), "now": now} for row in draft_list.read_drafts(work_dir, now)]
