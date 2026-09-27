from datetime import UTC, datetime

from agent_tools.chair_report import Deps, echo_line, format_status, lands_this_tick, write_status
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


def test_a_launch_on_a_lane_host_names_the_host():
    line = format_status(_facts(), [], [_result("launch_epic", "done", initiative="x", host="jarvis")], NOW)
    assert "launched: epic:x@jarvis" in line
    assert "launch_epic" not in line


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


def test_drafts_3_prints_drafts_3_after_lands():
    line = format_status(dict(_facts(), drafts=3), [], [], NOW)
    assert " | lands 0 | drafts 3 | limits " in line


def test_drafts_0_leaves_the_line_as_it_was():
    line = format_status(dict(_facts(), drafts=0), [], [], NOW)
    assert line == format_status(_facts(), [], [], NOW)
    assert "drafts" not in line


def test_a_new_missing_repo_is_named_in_the_line():
    line = format_status(dict(_facts(), missing_repos=["/repo/a"]), [], [], NOW)
    assert " | skipped missing repo /repo/a | needs chair: none" in line


def test_no_missing_repos_leaves_the_line_as_it_was():
    line = format_status(dict(_facts(), missing_repos=[]), [], [], NOW)
    assert line == format_status(_facts(), [], [], NOW)
    assert "missing repo" not in line


def test_standby_line_with_an_until_says_who_holds_the_chair_until_when():
    actions = [{"kind": "standby", "holder": "chair-7@box-1:42", "host": "box-1", "until": "2026-09-26T15:00:00+00:00"}]
    line = format_status(_facts(), actions, [_landed("standby", "recorded")], NOW)
    assert "standby: held by chair-7 until 2026-09-26T15:00:00+00:00" in line


def test_echo_line_flushes_stdout_after_the_line(monkeypatch):
    class Stub:
        def __init__(self):
            self.calls = []

        def write(self, text):
            self.calls.append(("write", text))

        def flush(self):
            self.calls.append(("flush", ""))

    stub = Stub()
    monkeypatch.setattr("sys.stdout", stub)
    write_status("chair tick", Deps(echo=echo_line))
    assert stub.calls[-1] == ("flush", "")
    assert ("write", "chair tick") in stub.calls
