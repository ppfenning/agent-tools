from datetime import UTC, datetime
from pathlib import Path

from agent_tools.inbox import InboxItem
from agent_tools.inbox_drafts import drafts_to_items, load_draft_records

STALE_BY = "chair (stale since 2026-10-01T00:00:00Z: no lane for 4 days)"


def test_dated_draft_becomes_item_with_reason_and_both_commands() -> None:
    record = {"id": "stale-cleanup", "proposed_by": STALE_BY, "age_seconds": 90000, "now": "2026-10-05T12:00:00Z"}
    assert drafts_to_items([record]) == (
        InboxItem(
            id="beff4291",
            kind="draft",
            created_at=datetime(2026, 10, 4, 11, 0, tzinfo=UTC),
            what="draft stale-cleanup awaits approve or decline",
            evidence=f"proposed by {STALE_BY}",
            accept_cmd=("cox", "route", "approve", "stale-cleanup"),
            deny_cmd=("cox", "route", "decline", "stale-cleanup", "--reason", "declined from inbox"),
        ),
    )


def test_undated_draft_is_kept_at_the_epoch() -> None:
    record = {"id": "hand-edited", "proposed_by": "pat", "age_seconds": None, "now": "2026-10-05T12:00:00Z"}
    assert drafts_to_items([record]) == (
        InboxItem(
            id="59aafd32",
            kind="draft",
            created_at=datetime(1970, 1, 1, tzinfo=UTC),
            what="draft hand-edited awaits approve or decline",
            evidence="proposed by pat; age unknown: proposed_at missing or unparseable",
            accept_cmd=("cox", "route", "approve", "hand-edited"),
            deny_cmd=("cox", "route", "decline", "hand-edited", "--reason", "declined from inbox"),
        ),
    )


def test_load_draft_records_returns_plain_rows(tmp_path: Path) -> None:
    (tmp_path / "d1").mkdir()
    (tmp_path / "d1" / "initiative.md").write_text(
        "---\nid: d1\ndraft: true\nproposed_by: steward\nproposed_at: 2026-10-04T11:00:00Z\n---\n", encoding="utf-8"
    )
    assert load_draft_records(tmp_path, "2026-10-05T12:00:00Z") == [
        {"id": "d1", "proposed_by": "steward", "age_seconds": 90000, "now": "2026-10-05T12:00:00Z"}
    ]
