from datetime import UTC, datetime

from agent_tools.inbox import InboxItem, item_id
from agent_tools.inbox_approvals import approvals_to_items


def test_record_becomes_approval_item():
    record = {"id": "fix-parser", "proposed_at": "2026-10-04T09:30:00+00:00", "reasoning": "arbiter sided with the builder"}
    assert approvals_to_items([record]) == (
        InboxItem(
            id=item_id("approval", "fix-parser"),
            kind="approval",
            created_at=datetime(2026, 10, 4, 9, 30, tzinfo=UTC),
            what="initiative fix-parser awaits routing approval",
            evidence="arbiter sided with the builder",
            accept_cmd=("cox", "route", "approve", "fix-parser"),
            deny_cmd=("cox", "route", "decline", "fix-parser", "--reason", "declined from the inbox"),
        ),
    )
