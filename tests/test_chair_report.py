from datetime import UTC, datetime

from agent_tools.chair_read_quarantined import RUNAWAY_CAUSE
from agent_tools.chair_report import (
    Deps,
    _five_hour,
    _weekly,
    echo_line,
    format_status,
    housekeeping_fragment,
    lands_this_tick,
    needs_chair_items,
    write_status,
)
from agent_tools.notify import Notification

NOW = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)  # 14:05 EDT


def _facts(hard_stop=False, weekly=0.61, five=0.42, last_housekeeping_at=None, window_start_day=None,
           five_hour_source="meter", weekly_source="meter", review_prs=None, idle_stall=None):
    return {
        **({} if review_prs is None else {"review_prs": review_prs}),
        **({} if idle_stall is None else {"idle_stall": idle_stall}),
        "limits": {"hard_stop": hard_stop, "weekly_fraction": weekly, "hard_stop_fraction": 0.9,
                   "launch_cap": 2, "go_degraded": False, "five_hour_fraction": five,
                   "window_start_day": window_start_day, "window_source": five_hour_source,
                   "weekly_source": weekly_source},
        "dispatch": {"max_in_flight": 4, "live_runs": 2},
        "last_housekeeping_at": last_housekeeping_at,
    }


def _landed(kind="land", status="landed"):
    return {"action": {"kind": kind}, "status": status, "reason": ""}


def test_holding_line_carries_lanes_lands_limits_and_needs():
    actions = [{"kind": "needs_chair", "initiative": "epic-a", "cause": "harness"}]
    line = format_status(_facts(), actions, [_landed()], NOW)
    assert line == (
        "chair 09-26 14:05 EDT | lanes 2/4 | lands 1 | limits 5h 42% (meter) weekly 61%/90% (meter) | holding"
        " | needs chair: epic-a:harness | housekeeping never"
    )


_BASELINE = (
    "chair 09-26 14:05 EDT | lanes 2/4 | lands 1 | limits 5h 42% (meter) weekly 61%/90% (meter) | holding"
    " | needs chair: epic-a:harness | housekeeping never"
)
_NEEDS = [{"kind": "needs_chair", "initiative": "epic-a", "cause": "harness"}]


def test_an_open_idle_stall_appears_after_limits():
    facts = _facts(idle_stall={"open_diagnosis": "host jarvis ssh check failed"})
    assert format_status(facts, _NEEDS, [_landed()], NOW) == (
        "chair 09-26 14:05 EDT | lanes 2/4 | lands 1 | limits 5h 42% (meter) weekly 61%/90% (meter)"
        " | stall: host jarvis ssh check failed | holding | needs chair: epic-a:harness | housekeeping never"
    )


def test_an_idle_stall_with_no_open_diagnosis_leaves_the_line_as_it_was():
    facts = _facts(idle_stall={"open_diagnosis": None})
    assert format_status(facts, _NEEDS, [_landed()], NOW) == _BASELINE


def test_facts_with_no_idle_stall_key_leave_the_line_as_it_was():
    assert "idle_stall" not in _facts()
    assert format_status(_facts(), _NEEDS, [_landed()], NOW) == _BASELINE


def test_needs_chair_items_puts_runaway_entries_first_and_bare():
    actions = [
        {"kind": "needs_chair", "initiative": "a", "cause": "budget"},
        {"kind": "needs_chair", "initiative": "b", "cause": RUNAWAY_CAUSE},
        {"kind": "needs_chair", "initiative": "c", "cause": "scope"},
    ]
    assert needs_chair_items(actions) == ["RUNAWAY b", "needs chair: a:budget", "needs chair: c:scope"]


def test_standby_line_names_the_holder_and_host():
    actions = [{"kind": "standby", "holder": "chair-7", "host": "box-1"}]
    line = format_status(_facts(), actions, [_landed("standby", "recorded")], NOW)
    assert "standby holder=chair-7 host=box-1" in line
    assert "lands 0" in line and "needs chair: none" in line


def test_hard_stop_line_says_hard_stop():
    line = format_status(_facts(hard_stop=True, weekly=0.95), [], [], NOW)
    assert "weekly 95%/90% (meter) hard stop" in line
    assert "hard stop" not in format_status(_facts(), [], [], NOW)


def test_a_window_start_day_appears_since_it_in_the_weekly_fragment():
    line = format_status(_facts(window_start_day="Sun 04:00"), [], [], NOW)
    assert "weekly 61%/90% (meter) since Sun 04:00" in line


def test_five_hour_names_a_meter_source():
    assert _five_hour({"five_hour_fraction": 0.42, "window_source": "meter"}) == "5h 42% (meter)"


def test_five_hour_names_an_est_source():
    assert _five_hour({"five_hour_fraction": 0.42, "window_source": "est"}) == "5h 42% (est)"


def test_weekly_names_a_meter_source():
    limits = {"weekly_fraction": 0.61, "hard_stop_fraction": 0.9, "weekly_source": "meter", "window_start_day": None}
    assert _weekly(limits) == "weekly 61%/90% (meter)"


def test_weekly_names_an_est_source():
    limits = {"weekly_fraction": 0.61, "hard_stop_fraction": 0.9, "weekly_source": "est", "window_start_day": None}
    assert _weekly(limits) == "weekly 61%/90% (est)"


def test_store_fed_sources_render_meter_store_in_both_limit_fragments():
    line = format_status(_facts(five=0.12, weekly=0.13, five_hour_source="meter, store", weekly_source="meter, store"),
                         [], [], NOW)
    assert "5h 12% (meter, store) weekly 13%/90% (meter, store)" in line


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


def test_a_not_landed_land_phase_appears_as_failed_with_its_phase():
    results = [_result("land_phase", "not_landed", phase="p2", initiative="epic-a")]
    line = format_status(_facts(), [], results, NOW)
    assert " | failed: land_phase:epic-a/p2 | needs chair: none" in line


def test_a_quiet_tick_names_no_launch_and_no_failure():
    line = format_status(_facts(), [], [_landed(), _result("launch_epic", "fenced", initiative="x")], NOW)
    assert "launched:" not in line and "failed:" not in line


def test_a_fetch_exit_done_names_the_run_and_host():
    line = format_status(_facts(), [], [_result("fetch_exit", "done", run="epic-a-1", host="jarvis")], NOW)
    assert "fetched: epic-a-1 from jarvis" in line


def test_a_fetch_exit_done_with_no_host_says_another_machine():
    line = format_status(_facts(), [], [_result("fetch_exit", "done", run="epic-a-1", host=None)], NOW)
    assert "fetched: epic-a-1 from on another machine" in line


def test_a_fetch_exit_failed_appears_as_failed_not_fetched():
    line = format_status(_facts(), [], [_result("fetch_exit", "failed", run="epic-a-1")], NOW)
    assert "fetched:" not in line
    assert "failed: fetch_exit:?" in line


def test_a_tick_with_no_fetch_exit_result_is_unchanged():
    results = [_result("rescue", "done", initiative="epic-a", task_id="t1"), _result("retry", "done", initiative="epic-b")]
    line = format_status(_facts(), [], results, NOW)
    assert "fetched:" not in line


def test_a_dry_run_line_names_what_it_would_do():
    line = format_status(_facts(), [], [_result("launch_epic", "dry_run", initiative="x")], NOW)
    assert "dry-run | would: launch_epic:x" in line


def test_a_land_counts_only_when_landed():
    assert lands_this_tick([_landed(), _landed(status="not_landed"), _landed("clear_branches", "done")]) == 1


def test_a_land_phase_counts_the_same_as_a_land():
    assert lands_this_tick([_landed("land_phase"), _landed("land_phase", status="not_landed")]) == 1


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


def test_housekeeping_3_hours_old_renders_3h():
    assert housekeeping_fragment("2026-09-26T15:05:00+00:00", NOW) == "housekeeping 3h"


def test_housekeeping_12_minutes_old_renders_12m():
    assert housekeeping_fragment("2026-09-26T17:53:00+00:00", NOW) == "housekeeping 12m"


def test_housekeeping_50_hours_old_renders_2d():
    assert housekeeping_fragment("2026-09-24T16:05:00+00:00", NOW) == "housekeeping 2d"


def test_housekeeping_none_renders_never():
    assert housekeeping_fragment(None, NOW) == "housekeeping never"


def test_housekeeping_naive_timestamp_renders_never():
    assert housekeeping_fragment("2026-09-26T15:05:00", NOW) == "housekeeping never"


def test_status_line_with_a_housekeeping_timestamp_carries_the_fragment():
    line = format_status(_facts(last_housekeeping_at="2026-09-26T15:05:00+00:00"), [], [], NOW)
    assert "housekeeping 3h" in line


def _review_pr(task_id, url):
    return {"initiative": "epic-a", "phase": "p1", "task_id": task_id, "repo": "r", "url": url,
            "state": "unknown", "merged_at": None}


def test_two_awaiting_review_prs_name_both_urls_in_the_status_line():
    prs = [_review_pr("t-1", "https://x/pull/1"), _review_pr("t-2", "https://x/pull/2")]
    line = format_status(_facts(review_prs=prs), [], [], NOW)
    assert "review: 2 awaiting (t-1 https://x/pull/1, t-2 https://x/pull/2)" in line


def test_no_awaiting_review_prs_omits_the_review_part():
    assert "review:" not in format_status(_facts(review_prs=[]), [], [], NOW)
