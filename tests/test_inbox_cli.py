import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent_tools.commands import build_parser
from agent_tools.inbox import item_id
from agent_tools.inbox_cli import (
    INBOX_GROUP,
    USAGE,
    LocalTime,
    argv_of,
    gather,
    handler,
    run_inbox,
    subprocess_runner,
)

DRAFTS = [{"id": "d1", "proposed_by": "chair", "age_seconds": 86400, "now": "2026-10-05T00:00:00+00:00"}]
APPROVALS = [{"id": "init-a", "proposed_at": "2026-10-02T00:00:00+00:00", "reasoning": "needs a human"}]
PRS = [
    {
        "url": "https://github.com/o/r/pull/7",
        "pr": {
            "number": 7,
            "title": "Fix it",
            "createdAt": "2026-10-03T00:00:00Z",
            "state": "OPEN",
            "isDraft": False,
            "reviewDecision": "REVIEW_REQUIRED",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        },
    }
]
APPROVAL_ID = item_id("approval", "init-a")
PR_ID = item_id("pr", "https://github.com/o/r/pull/7")
DRAFT_ID = item_id("draft", "d1")


class FakeRun:
    def __init__(self, code: int = 0) -> None:
        self.code, self.calls = code, []

    def __call__(self, argv) -> int:
        self.calls.append(tuple(argv))
        return self.code


def invoke(argv, run, tz=UTC) -> int:
    return run_inbox(
        argv, load_drafts=lambda: DRAFTS, load_approvals=lambda: APPROVALS, load_prs=lambda: PRS, run=run, tz=tz
    )


def test_list_text_and_json_oldest_first(capsys):
    run = FakeRun()
    assert invoke([], run, timezone(timedelta(hours=2))) == 0
    assert capsys.readouterr().out == (
        f"{APPROVAL_ID}  approval  2026-10-02 02:00  initiative init-a awaits routing approval\n"
        "    needs a human\n"
        f"{PR_ID}  pr  2026-10-03 02:00  #7 Fix it\n"
        "    checks: 1 passed, 0 failed, 0 pending; waiting on review, then merge\n"
        f"{DRAFT_ID}  draft  2026-10-04 02:00  draft d1 awaits approve or decline\n"
        "    proposed by chair\n"
    )
    assert invoke(["--json"], run) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [APPROVAL_ID, PR_ID, DRAFT_ID]
    assert [r["kind"] for r in rows] == ["approval", "pr", "draft"]
    assert run.calls == []


def test_accept_runs_the_accept_command_and_returns_its_code(capsys):
    run = FakeRun(3)
    assert invoke(["accept", PR_ID], run) == 3
    assert run.calls == [("gh", "pr", "merge", "https://github.com/o/r/pull/7", "--squash", "--delete-branch")]
    assert capsys.readouterr().out == "gh pr merge https://github.com/o/r/pull/7 --squash --delete-branch\n"


def test_deny_runs_the_deny_command_and_returns_its_code(capsys):
    run = FakeRun(0)
    assert invoke(["deny", APPROVAL_ID[:6]], run) == 0
    assert run.calls == [("cox", "route", "decline", "init-a", "--reason", "declined from the inbox")]
    assert capsys.readouterr().out == "cox route decline init-a --reason 'declined from the inbox'\n"


def test_unknown_id_exits_nonzero_and_runs_nothing(capsys):
    run = FakeRun()
    assert invoke(["accept", "zzzzzzzz"], run) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "cox inbox: no inbox item with id 'zzzzzzzz'\n"
    assert run.calls == []


def test_unreadable_pr_is_reported_and_exits_nonzero(capsys):
    broken = [{"url": "https://github.com/o/r/pull/9", "pr": {"error": "gh auth login required"}}]
    code = run_inbox(
        [], load_drafts=lambda: [], load_approvals=lambda: [], load_prs=lambda: broken, run=FakeRun(), tz=UTC
    )
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == "Inbox is empty.\n"
    assert captured.err == "cox inbox: could not read PR https://github.com/o/r/pull/9: gh auth login required\n"


def test_bad_arguments_exit_2_before_any_loader_runs(capsys):
    def boom():
        raise AssertionError("loader ran")

    code = run_inbox(["bogus"], load_drafts=boom, load_approvals=boom, load_prs=boom, run=FakeRun(), tz=UTC)
    assert code == 2
    assert capsys.readouterr().err == f"{USAGE}\n"


def test_draft_and_approval_for_one_initiative_list_once():
    items = gather(DRAFTS, [{"id": "d1", "proposed_at": "2026-10-01T00:00:00+00:00"}], [])
    assert [(i.id, i.kind) for i in items] == [(DRAFT_ID, "draft")]


@pytest.fixture
def berlin(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_local_time_takes_each_items_own_dst_offset(berlin):
    winter, summer = datetime(2026, 1, 15, 12, tzinfo=UTC), datetime(2026, 7, 15, 12, tzinfo=UTC)
    assert [f"{d.astimezone(LocalTime()):%H:%M}" for d in (winter, summer)] == ["13:00", "14:00"]


def test_subprocess_runner_reports_exit_codes():
    assert subprocess_runner([sys.executable, "-c", "raise SystemExit(4)"]) == 4
    assert subprocess_runner([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"]) == 143
    assert subprocess_runner(["/nonexistent/cox-inbox-missing"]) == 127


def test_inbox_group_parses_to_run_inbox_argv():
    group = build_parser([], [INBOX_GROUP], argparse.ArgumentParser(prog="cox").add_subparsers())["inbox"]
    assert [argv_of(group.parse_args(a)) for a in ([], ["--json"], ["accept", "1a2b"], ["deny", "9f"])] == [
        [], ["--json"], ["accept", "1a2b"], ["deny", "9f"]
    ]
    assert group.parse_args([]).fn is handler
