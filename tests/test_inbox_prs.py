from datetime import UTC, datetime

from agent_tools.inbox import InboxItem, item_id
from agent_tools.inbox_prs import pr_errors, prs_to_items

URL = "https://github.com/acme/widgets/pull/42"
PR = {
    "number": 42,
    "title": "Add the widget",
    "url": URL,
    "createdAt": "2026-10-05T04:12:00Z",
    "state": "OPEN",
    "isDraft": False,
    "reviewDecision": "APPROVED",
    "mergeStateStatus": "UNSTABLE",
    "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}, {"state": "PENDING"}],
}
RECORDS = [
    {"url": URL, "repo": "acme/widgets", "pr": PR},
    {"url": "https://github.com/acme/widgets/pull/43", "pr": {**PR, "isDraft": True}},
    {"url": "https://github.com/acme/widgets/pull/44", "pr": {**PR, "reviewDecision": "CHANGES_REQUESTED"}},
    {"url": "https://github.com/acme/widgets/pull/45", "pr": {**PR, "state": "MERGED"}},
    {"url": "https://github.com/acme/gadgets/pull/7", "pr": {"error": "gh: not authenticated"}},
]


def test_prs_to_items_keeps_only_prs_waiting_on_a_merge():
    assert prs_to_items(RECORDS) == (
        InboxItem(
            id=item_id("pr", URL),
            kind="pr",
            created_at=datetime(2026, 10, 5, 4, 12, tzinfo=UTC),
            what="#42 Add the widget",
            evidence="checks: 1 passed, 1 failed, 1 pending; blocked: 1 checks failing",
            accept_cmd=("gh", "pr", "merge", URL, "--squash", "--delete-branch"),
            deny_cmd=("gh", "pr", "close", URL),
        ),
    )


def test_pr_errors_names_each_unreadable_pr():
    assert pr_errors(RECORDS) == ("https://github.com/acme/gadgets/pull/7: gh: not authenticated",)
