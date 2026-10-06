import json
from datetime import UTC, datetime

from agent_tools.chair_read_record import ACTION_LOG
from agent_tools.inbox import InboxItem, item_id
from agent_tools.inbox_needs_chair import load_needs_chair_records, needs_chair_to_items, open_needs_chair

SET_STATE = ("python", "-m", "harness.store_cli", "set-state")

# Keys chair_exec logs: needs_chair has kind, initiative, task_id, phase, cause, reason, epoch; an executed action
# adds status. A land has task_id and no phase; a land_phase has initiative and phase and no task_id.
FULL = {
    "kind": "needs_chair",
    "ts": "2026-10-05T12:00:00+00:00",
    "initiative": "init",
    "task_id": "t1",
    "cause": "code",
    "reason": "tests fail on main",
    "remedy": "re-ground",
    "command": [*SET_STATE, "init", "t1", "ready", "--by", "chair"],
}
PHASE_ROW = {"kind": "needs_chair", "ts": "2026-10-05T12:00:00Z", "initiative": "init", "phase": "p1", "cause": "land"}


def test_full_record_gives_one_full_item():
    assert needs_chair_to_items([FULL]) == (
        InboxItem(
            id=item_id("needs-chair", "t1"),
            kind="needs-chair",
            created_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
            what="t1: code",
            evidence="tests fail on main",
            accept_cmd=(*SET_STATE, "init", "t1", "ready", "--by", "chair"),
            deny_cmd=(*SET_STATE, "init", "t1", "dropped", "--by", "chair"),
        ),
    )


def test_record_without_command_falls_back_to_reground():
    record = {"kind": "needs_chair", "ts": "2026-10-05T12:00:00Z", "initiative": "init", "task_id": "t1", "cause": "stale"}
    (item,) = needs_chair_to_items([record])
    assert (item.accept_cmd, item.evidence) == ((*SET_STATE, "init", "t1", "ready", "--by", "chair"), "")


def test_initiative_grain_record_uses_empty_run_task_and_repo():
    record = {"kind": "needs_chair", "ts": "2026-10-05T12:00:00+00:00", "initiative": "init", "cause": "runaway"}
    (item,) = needs_chair_to_items([record])
    assert item.id == item_id("needs-chair", "init")
    assert item.deny_cmd == (*SET_STATE, "", "", "dropped", "--by", "chair")


def test_phase_record_is_keyed_by_initiative_and_phase():
    (item,) = needs_chair_to_items([PHASE_ROW, {**PHASE_ROW, "ts": "2026-10-05T13:00:00Z", "cause": "checks"}])
    assert (item.id, item.what) == (item_id("needs-chair", "init/p1"), "init/p1: checks")


def test_two_records_for_one_target_give_one_item():
    newer = {**FULL, "ts": "2026-10-05T13:00:00+00:00", "cause": "review", "reason": "newer"}
    (item,) = needs_chair_to_items([FULL, newer])
    assert (item.what, item.evidence) == ("t1: review", "newer")


def test_other_targets_stay_separate():
    other_phase = {**PHASE_ROW, "phase": "p2"}
    unnamed = {"kind": "needs_chair", "ts": "2026-10-05T12:00:00Z", "cause": "x"}
    later_unnamed = {**unnamed, "ts": "2026-10-05T13:00:00Z"}
    rows = [FULL, {**FULL, "task_id": "t2"}, PHASE_ROW, other_phase, unnamed, later_unnamed]
    assert len({i.id for i in needs_chair_to_items(rows)}) == 6


def test_record_closed_by_later_landed_land_is_dropped(tmp_path):
    other = {**FULL, "task_id": "t2"}
    land = {"kind": "land", "ts": "2026-10-05T14:00:00+00:00", "initiative": "init", "task_id": "t1", "status": "landed"}
    lines = [json.dumps(row) for row in (FULL, other, land)]
    (tmp_path / ACTION_LOG).write_text("\n".join([*lines, "", "not json"]) + "\n", encoding="utf-8")
    assert load_needs_chair_records(tmp_path) == [other]


def test_busy_fenced_or_refused_land_keeps_the_record_open():
    attempts = [{"kind": "land", "task_id": "t1", "status": s} for s in ("busy", "fenced", "refused", "not_landed")]
    assert open_needs_chair([FULL, *attempts]) == [FULL]


def test_land_phase_of_same_initiative_and_phase_closes_a_record_with_no_task_id():
    land = {"kind": "land_phase", "initiative": "init", "phase": "p1", "status": "landed"}
    assert open_needs_chair([PHASE_ROW, land]) == []


def test_land_phase_of_another_phase_or_initiative_leaves_it_open():
    other_phase = {"kind": "land_phase", "initiative": "init", "phase": "p2", "status": "landed"}
    other_init = {"kind": "land_phase", "initiative": "other", "phase": "p1", "status": "landed"}
    assert open_needs_chair([PHASE_ROW, other_phase, other_init]) == [PHASE_ROW]


def test_relaunch_of_the_initiative_closes_an_initiative_row_and_an_earlier_land_closes_nothing():
    row = {"kind": "needs_chair", "initiative": "init", "cause": "runaway"}
    early = {"kind": "land", "task_id": "t1", "status": "landed"}
    relaunch = {"kind": "relaunch", "initiative": "init", "status": "done"}
    assert open_needs_chair([early, FULL, row, relaunch]) == [FULL]


HOST_ROW = {"kind": "needs_chair", "ts": "2026-10-05T12:00:00Z", "host": "mac1", "cause": "login_lapsed", "url": "https://x/a"}


def test_host_rows_from_every_tick_give_one_named_item():
    ticks = [{**HOST_ROW, "ts": f"2026-10-05T12:0{m}:00Z", "status": "recorded"} for m in range(3)]
    (item,) = needs_chair_to_items(open_needs_chair(ticks))
    assert (item.id, item.what) == (item_id("needs-chair", "host/mac1"), "host mac1: login_lapsed https://x/a")


def test_host_row_closes_on_a_later_good_login_check_of_that_host_only():
    good = {"kind": "check_login", "host": "mac1", "status": "recorded", "reason": json.dumps({"login_ok": True})}
    bad = {**good, "reason": json.dumps({"login_ok": False})}
    other = {**good, "host": "mac2"}
    assert open_needs_chair([HOST_ROW, good]) == []
    assert open_needs_chair([HOST_ROW, bad, other]) == [HOST_ROW]


def test_relaunch_closes_a_phase_row_of_its_initiative_and_phase():
    relaunch = {"kind": "relaunch", "initiative": "init", "status": "done"}
    assert open_needs_chair([PHASE_ROW, {**relaunch, "phase": "p1"}]) == []
    assert open_needs_chair([PHASE_ROW, relaunch]) == []
    assert open_needs_chair([PHASE_ROW, {**relaunch, "phase": "p2"}, {**relaunch, "status": "failed"}]) == [PHASE_ROW]


def test_missing_log_gives_no_records(tmp_path):
    assert load_needs_chair_records(tmp_path) == []
