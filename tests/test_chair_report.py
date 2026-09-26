from datetime import UTC, datetime

from agent_tools.chair_report import Deps, format_status, lands_this_tick, write_status
from agent_tools.notify import Notification

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)  # 14:05 EDT


def _facts(hard_stop=False, weekly=0.61, five=0.42):
    return {
        "limits": {"hard_stop": hard_stop, "weekly_fraction": weekly, "hard_stop_fraction": 0.9,
                   "launch_cap": 2, "go_degraded": False, "five_hour_fraction": five},
        "dispatch": {"max_in_flight": 4, "live_runs": 2},
    }


def _landed(kind="land", status="landed"):
    return {"action": {"kind": kind}, "status": status, "reason": ""}


def test_holding_line_carries_lanes_lands_limits_and_needs():
    actions = [{"kind": "needs_chair", "initiative": "epic-a", "cause": "harness"}]
    line = format_status(_facts(), actions, [_landed()], NOW)
    assert line == (
        "chair 09-26 14:05 EDT | lanes 2/4 | lands 1 | limits 5h 42% weekly 61%/90% | holding"
        " | needs chair: epic-a:harness"
    )


def test_standby_line_names_the_holder_and_host():
    actions = [{"kind": "standby", "holder": "chair-7", "host": "box-1"}]
    line = format_status(_facts(), actions, [_landed("standby", "recorded")], NOW)
    assert "standby holder=chair-7 host=box-1" in line
    assert "lands 0" in line and "needs chair: none" in line


def test_hard_stop_line_says_hard_stop():
    line = format_status(_facts(hard_stop=True, weekly=0.95), [], [], NOW)
    assert "weekly 95%/90% hard stop" in line
    assert "hard stop" not in format_status(_facts(), [], [], NOW)


def test_dry_run_line_says_dry_run():
    line = format_status(_facts(), [{"kind": "land"}], [_landed("land", "dry_run")], NOW)
    assert " | dry-run | " in line


def _result(kind, status, **fields):
    return {"action": {"kind": kind, **fields}, "status": status, "reason": ""}


def test_a_rescue_that_ran_appears_as_launched():
    results = [_result("rescue", "done", initiative="epic-a", task_id="t1"), _result("retry", "done", initiative="epic-b")]
    line = format_status(_facts(), [], results, NOW)
    assert " | launched: rescue:epic-a, retry:epic-b | needs chair: none" in line


def test_a_not_landed_land_appears_as_failed():
    results = [_result("land", "not_landed", task_id="t1", initiative="epic-a"), _result("pull", "refused", initiative="epic-c")]
    line = format_status(_facts(), [], results, NOW)
    assert " | failed: land:t1, pull:epic-c | needs chair: none" in line


def test_a_quiet_tick_names_no_launch_and_no_failure():
    line = format_status(_facts(), [], [_landed(), _result("launch_epic", "fenced", initiative="x")], NOW)
    assert "launched:" not in line and "failed:" not in line


def test_a_dry_run_line_names_what_it_would_do():
    line = format_status(_facts(), [], [_result("launch_epic", "dry_run", initiative="x")], NOW)
    assert "dry-run | would: launch_epic:x" in line


def test_a_land_counts_only_when_landed():
    assert lands_this_tick([_landed(), _landed(status="not_landed"), _landed("clear_branches", "done")]) == 1


def test_write_status_notifies_only_when_given():
    printed, sent = [], []
    write_status("line", Deps(echo=printed.append))
    write_status("line", Deps(echo=printed.append, notify=sent.append))
    assert printed == ["line", "line"]
    assert sent == [Notification("chair tick", "line", "low")]
