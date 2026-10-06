from datetime import UTC, datetime

from agent_tools.chair_read_record import ACTION_LOG, action_line
from agent_tools.inbox import InboxItem, item_id
from agent_tools.inbox_refused import load_refused_rows, refused_to_items

REFUSED = {
    "ts": "2026-10-05T01:02:03Z",
    "kind": "land",
    "run": "run-1",
    "task_id": "t1",
    "repo": "/r/app",
    "status": "refused",
    "reason": "  tests failed on main  ",
}


def test_one_full_item_from_a_refused_land_row():
    assert refused_to_items([REFUSED]) == (
        InboxItem(
            id=item_id("refused-command", "run-1\0t1"),
            kind="refused-command",
            created_at=datetime(2026, 10, 5, 1, 2, 3, tzinfo=UTC),
            what="land of t1 in run-1 was refused",
            evidence="tests failed on main",
            accept_cmd=("cox", "runs", "land", "run-1", "--repo", "/r/app", "--task", "t1", "--apply"),
            deny_cmd=("python", "-m", "harness.store_cli", "set-state", "run-1", "t1", "dropped", "--by", "chair"),
        ),
    )


def test_a_later_landed_row_drops_the_refusal():
    landed = {**REFUSED, "ts": "2026-10-05T02:00:00Z", "status": "landed", "reason": "ok"}
    assert refused_to_items([REFUSED, landed]) == ()


def test_two_refusals_for_one_run_and_task_give_the_newest():
    newer = {**REFUSED, "ts": "2026-10-05T03:00:00Z", "reason": "second"}
    (item,) = refused_to_items([newer, REFUSED])
    assert (item.evidence, item.created_at) == ("second", datetime(2026, 10, 5, 3, 0, tzinfo=UTC))


def test_evidence_is_cut_to_600_characters():
    (item,) = refused_to_items([{**REFUSED, "reason": "x" * 700}])
    assert item.evidence == "x" * 600


def test_other_kinds_and_statuses_make_no_item():
    rows = [{**REFUSED, "kind": "land_phase"}, {**REFUSED, "status": "not_landed"}]
    assert refused_to_items(rows) == ()


def test_load_refused_rows_reads_the_log_and_drops_landed(tmp_path):
    landed = {**REFUSED, "task_id": "t2", "status": "landed"}
    refused_t2 = {**REFUSED, "task_id": "t2", "ts": "2026-10-05T00:00:00Z"}
    lines = [action_line(a, 1, a["ts"]) for a in (REFUSED, refused_t2, landed)]
    (tmp_path / ACTION_LOG).write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert [r["task_id"] for r in load_refused_rows(tmp_path)] == ["t1"]
    assert load_refused_rows(tmp_path / "missing") == []
