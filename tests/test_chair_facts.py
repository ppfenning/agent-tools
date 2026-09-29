from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agent_tools import chair_smoke, pacing, queue_rows
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import (
    FactsDeps,
    dispatch_facts,
    gather_facts,
    harness_failures,
    lease_facts,
    limits_facts,
    new_missing_repos,
)
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_read_attempts import with_stored_rescues
from agent_tools.chair_types import Facts
from agent_tools.usage_window import WeeklyReset, weekly_window_start

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
POLICY = pacing.Policy(
    pace_thresholds=(1.2, 1.5, 2.0),
    tier_ladder=("deep", "standard", "cheap"),
    effort_ladder=("high", "low"),
    min_headroom_usd=1.0,
    weekly_hard_stop_fraction=0.85,
)
MINE = lease_holder("s", 7, "h")
HARNESS = {"run": "i-1", "phase": "p1", "task": "a", "cause": "harness"}
QUARANTINED = {"initiative": "i", "phase": "p1", "task": "a"}
# The keys `runs_stranded._row` emits, and no others: a real stranded row carries no initiative.
STRANDED = {"run": "i-3", "task": "s", "phase": "p1", "branch": "i/s", "remedy": None}
BARE = {"task_id": "a", "initiative": "i", "has_patch": False, "rescue_failed": False}


def _window(spent: float, hours: int) -> pacing.Window:
    return pacing.Window(NOW - timedelta(hours=1), NOW + timedelta(hours=hours - 1), spent, 100.0, 0.0, 0)


def _deps(
    weekly_spent: float = 10.0,
    attempts: tuple[dict, ...] = (HARNESS,),
    live: tuple[str, ...] = (),
    quarantined: tuple[dict, ...] = (QUARANTINED,),
    stranded: tuple[dict, ...] = (STRANDED,),
    has_patch: bool = False,
) -> FactsDeps:
    docket = {
        "initiatives": [{"id": "i", "started": True, "ready_tasks": [{"id": "a", "needs": []}], "landed": ["z"]}],
        "busy_lanes": 1,
        "max_in_flight": 2,
    }
    return FactsDeps(
        lease=lambda: {"holder": MINE, "host": "h", "epoch": 4, "released": False, "stale": False},
        window=lambda: _window(1.0, 5),
        weekly=lambda: _window(weekly_spent, 168),
        policy=lambda: POLICY,
        docket=lambda: docket,
        approved=lambda: [{"id": "a", "initiative": "i", "repo": "r", "phase": "p", "phase_done": True, "needs": [], "run": "i-1", "needs_fetch": True}],
        quarantined=lambda: quarantined,
        stranded=lambda: stranded,
        attempts=lambda: attempts,
        has_patch=lambda initiative, task: has_patch,
        live_initiatives=lambda: live,
        intake=lambda: ["old", "new"],
        work_store_ready=lambda: True,
        sources_configured=lambda: False,
        session="s",
        pid=7,
        host="h",
    )


def test_a_remote_unfetched_callables_mapping_appears_under_remote_unfetched():
    deps = replace(_deps(), remote_unfetched=lambda: {"i": "i-1"})
    assert gather_facts(deps, NOW)["remote_unfetched"] == {"i": "i-1"}


def test_an_absent_remote_unfetched_callable_gives_an_empty_mapping():
    assert gather_facts(_deps(), NOW)["remote_unfetched"] == {}


def test_a_newest_run_host_callables_mapping_appears_under_newest_run_host():
    deps = replace(_deps(), newest_run_host=lambda: {"i": "jarvis"})
    assert gather_facts(deps, NOW)["newest_run_host"] == {"i": "jarvis"}


def test_an_absent_newest_run_host_callable_gives_an_empty_mapping():
    assert gather_facts(_deps(), NOW)["newest_run_host"] == {}


def test_home_maps_an_initiative_with_an_unlanded_approved_task_to_its_newest_run_host():
    deps = replace(_deps(), newest_run_host=lambda: {"i": "jarvis"})
    assert gather_facts(deps, NOW)["home"] == {"i": "jarvis"}


def test_home_has_no_entry_when_newest_run_host_is_unknown():
    assert gather_facts(_deps(), NOW)["home"] == {}


def test_a_history_callable_returning_a_timestamp_is_held_as_last_housekeeping_at():
    deps = replace(_deps(), history=lambda: "2026-09-25T00:00:00Z")
    assert gather_facts(deps, NOW)["last_housekeeping_at"] == "2026-09-25T00:00:00Z"


def test_absent_history_and_housekeeping_hours_callables_give_none_and_24():
    facts = gather_facts(_deps(), NOW)
    assert (facts["last_housekeeping_at"], facts["housekeeping_hours"]) == (None, 24)


def test_a_housekeeping_hours_callable_returning_6_gives_6_point_0():
    deps = replace(_deps(), housekeeping_hours=lambda: 6)
    assert gather_facts(deps, NOW)["housekeeping_hours"] == 6.0


def test_a_fake_stale_candidates_callable_returning_two_rows_gives_facts_holding_them_unchanged():
    rows = [{"initiative": "i", "task_id": "a"}, {"initiative": "i", "task_id": "b"}]
    deps = replace(_deps(), stale_candidates=lambda now: rows)
    assert gather_facts(deps, NOW)["stale_candidates"] == rows


def test_a_fake_stall_candidates_callable_returning_two_rows_gives_facts_holding_them_unchanged():
    rows = [{"run": "i-1", "initiative": "i"}, {"run": "i-2", "initiative": "i"}]
    deps = replace(_deps(), stall_candidates=lambda now: rows)
    assert gather_facts(deps, NOW)["stall_candidates"] == rows


def test_a_stale_days_callable_returning_10_gives_stale_days_10():
    deps = replace(_deps(), stale_days=lambda: 10)
    assert gather_facts(deps, NOW)["stale_days"] == 10


def test_an_absent_stale_days_callable_gives_7():
    assert gather_facts(_deps(), NOW)["stale_days"] == 7


def test_weekly_spend_of_85_percent_is_a_hard_stop_with_no_launches():
    limits = gather_facts(_deps(weekly_spent=85.0), NOW)["limits"]
    assert limits == {
        "hard_stop": True,
        "weekly_fraction": 0.85,
        "hard_stop_fraction": 0.85,
        "launch_cap": 0,
        "go_degraded": False,
        "five_hour_fraction": 0.01,
        "window_start_day": "Fri 07:00 EDT",
        "window_source": "est",
        "weekly_source": "est",
        "smoke_hold": None,
    }


def test_a_meter_backed_window_source_callable_reports_meter_in_limits():
    deps = replace(_deps(), window_source=lambda: "meter")
    assert gather_facts(deps, NOW)["limits"]["window_source"] == "meter"


def test_a_meter_backed_weekly_source_callable_reports_meter_in_limits():
    deps = replace(_deps(), weekly_source=lambda: "meter")
    assert gather_facts(deps, NOW)["limits"]["weekly_source"] == "meter"


def test_absent_source_callables_default_to_est_in_limits():
    limits = gather_facts(_deps(), NOW)["limits"]
    assert (limits["window_source"], limits["weekly_source"]) == ("est", "est")


def test_five_hour_fraction_is_the_assessments_spent_fraction():
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    assert limits_facts(assessment, POLICY, _window(10.0, 168), 2)["five_hour_fraction"] == 0.42


def test_a_saturday_night_eastern_reset_reads_as_saturday_not_as_the_utc_sunday():
    # The reset's start is 2026-11-01 03:00 UTC, a Sunday in UTC; the operator configured Saturday 23:00 Eastern.
    reset = WeeklyReset(weekday=5, hour=23, minute=0, tz="America/New_York")
    start = weekly_window_start(datetime(2026, 11, 2, 15, 0, tzinfo=UTC), reset)
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    window = pacing.Window(start, start + timedelta(days=7), 10.0, 100.0, 0.0, 0)
    assert limits_facts(assessment, POLICY, window, 2)["window_start_day"] == "Sat 23:00 EDT"


def test_no_weekly_window_gives_no_window_start_day():
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    assert limits_facts(assessment, POLICY, None, 2)["window_start_day"] is None


def test_a_written_smoke_hold_surfaces_its_cause_and_land_on_limits_facts(tmp_path):
    record = {
        "land": {"repo": "acme/widgets", "pr": 7, "commit": "abc123"},
        "failing_command": ["cox", "route", "context"],
        "tail": "Traceback...",
        "cause": "smoke_failed",
    }
    chair_smoke.write_hold(str(tmp_path), record)
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    smoke_hold = limits_facts(assessment, POLICY, None, 2, runs_dir=str(tmp_path))["smoke_hold"]
    assert smoke_hold is not None
    assert smoke_hold["cause"] == "smoke_failed"
    assert smoke_hold["land"] == record["land"]


def test_an_empty_runs_dir_reports_no_smoke_hold(tmp_path):
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    assert limits_facts(assessment, POLICY, None, 2, runs_dir=str(tmp_path))["smoke_hold"] is None


def test_weekly_spend_under_the_fraction_launches_up_to_max_in_flight():
    limits = gather_facts(_deps(weekly_spent=84.0), NOW)["limits"]
    assert (limits["hard_stop"], limits["launch_cap"]) == (False, 2)


def test_two_harness_attempts_give_harness_failures_of_two():
    attempts = [HARNESS, {**HARNESS, "cause": "code"}, {**HARNESS, "run": "i-2"}, {**HARNESS, "task": "other"}]
    assert harness_failures(attempts, ("i", "p1", "a")) == 2


def test_a_task_id_repeated_in_another_phase_is_counted_apart():
    assert harness_failures([HARNESS, {**HARNESS, "phase": "p2"}], ("i", "p1", "a")) == 1


def test_harness_attempts_off_the_current_body_are_not_counted():
    attempts = [{**HARNESS, "on_current_body": False}, {**HARNESS, "run": "i-2", "on_current_body": False}]
    assert harness_failures(attempts, ("i", "p1", "a")) == 0


def test_a_harness_attempt_with_no_on_current_body_key_still_counts():
    assert harness_failures([HARNESS], ("i", "p1", "a")) == 1


def test_the_cause_and_the_count_come_from_the_same_attempts():
    (q,) = gather_facts(_deps(attempts=({**HARNESS, "cause": "code"}, HARNESS), stranded=()), NOW)["quarantines"]
    assert q == {**BARE, "cause": "harness", "harness_failures": 1}


def test_a_quarantined_task_with_no_live_run_gives_a_quarantine_and_an_initiative():
    facts = gather_facts(_deps(stranded=()), NOW)
    assert facts["quarantines"] == [{**BARE, "cause": "harness", "harness_failures": 1}]
    assert [i["id"] for i in facts["initiatives"]] == ["i"]


def test_a_quarantined_task_whose_initiative_has_a_live_run_gives_neither():
    facts = gather_facts(_deps(live=("i",)), NOW)
    assert facts["quarantines"] == []
    assert facts["initiatives"] == []


def test_a_stranded_row_takes_its_initiative_from_its_run_and_a_live_run_drops_it():
    def stranded(live: tuple[str, ...]) -> list:
        return gather_facts(_deps(quarantined=(), live=live), NOW)["quarantines"]

    assert stranded(()) == [{**BARE, "task_id": "s", "cause": "stranded", "harness_failures": 0}]
    assert stranded(("i",)) == []


def test_a_fake_has_patch_that_returns_true_gives_has_patch_true():
    (q,) = gather_facts(_deps(has_patch=True, stranded=()), NOW)["quarantines"]
    assert q["has_patch"] is True


def test_a_fake_has_patch_that_returns_false_gives_has_patch_false():
    (q,) = gather_facts(_deps(has_patch=False, stranded=()), NOW)["quarantines"]
    assert q["has_patch"] is False


def test_a_rescue_failed_attempt_gives_rescue_failed_true_and_leaves_harness_failures_alone():
    (q,) = gather_facts(_deps(attempts=(HARNESS, {**HARNESS, "cause": "rescue_failed"}), stranded=()), NOW)["quarantines"]
    assert (q["rescue_failed"], q["harness_failures"]) == (True, 1)


def test_a_task_with_only_harness_attempts_gives_rescue_failed_false_and_harness_failures_of_one():
    (q,) = gather_facts(_deps(attempts=(HARNESS,), stranded=()), NOW)["quarantines"]
    assert (q["rescue_failed"], q["harness_failures"]) == (False, 1)


def test_an_approved_record_with_a_harness_attempt_and_a_kept_patch_plans_rescue():
    stranded = {**STRANDED, "run": "i-1", "task": "a"}
    facts = gather_facts(_deps(stranded=(stranded,), has_patch=True), NOW)
    assert [a["kind"] for a in plan_recover(facts) if a["kind"] in ("rescue", "retry", "needs_chair")] == ["rescue"]


def _kinds_with_stored_rescue(rescue_ts: str) -> list[str]:
    store = [{"run_id": "i-1", "task_id": "a", "phase_id": "p1", "ts": rescue_ts, "cause": "code"}]
    attempts = with_stored_rescues([{**HARNESS, "ts": "2026-09-10"}], store, {("i", "p1", "a"): "2026-09-10"})
    facts = gather_facts(_deps(attempts=tuple(attempts), stranded=(), has_patch=True), NOW)
    return [a["kind"] for a in plan_recover(facts) if a["kind"] in ("rescue", "retry", "needs_chair")]


def test_a_store_rescue_failed_row_on_the_current_body_sends_the_task_to_the_chair_not_rescue():
    assert _kinds_with_stored_rescue("2026-09-11") == ["needs_chair"]


def test_a_store_rescue_failed_row_older_than_the_current_bodys_first_attempt_still_plans_rescue():
    assert _kinds_with_stored_rescue("2026-09-09") == ["rescue"]


def test_a_failed_retry_reads_two_and_the_planner_hands_it_to_the_chair():
    def kinds(attempts: tuple[dict, ...]) -> list[str]:
        facts = gather_facts(replace(_deps(attempts=attempts), stranded=lambda: []), NOW)
        return [a["kind"] for a in plan_recover(facts) if a["kind"] in ("retry", "needs_chair")]

    assert kinds((HARNESS,)) == ["retry"]
    assert kinds((HARNESS, {**HARNESS, "run": "i-2"})) == ["needs_chair"]


def test_dispatch_facts_split_the_local_lanes_from_each_lane_hosts():
    facts = dispatch_facts({"max_in_flight": 4}, ["jarvis"], {"": 3, "jarvis": 1})
    assert facts["live_runs"] == 3
    assert facts["hosts"] == [{"name": "jarvis", "live_runs": 1, "weight": 1, "capabilities": []}]


def test_dispatch_facts_carry_a_hosts_own_capacity_only_when_it_has_one():
    facts = dispatch_facts({"max_in_flight": 4}, ["jarvis", "pi"], {}, {"jarvis": 8})
    assert facts["hosts"] == [
        {"name": "jarvis", "live_runs": 0, "capacity": 8, "weight": 1, "capabilities": []},
        {"name": "pi", "live_runs": 0, "weight": 1, "capabilities": []},
    ]


def test_dispatch_facts_carries_weight_and_capabilities_from_a_hosts_row():
    facts = dispatch_facts({"max_in_flight": 4}, ["h1"], {}, weight={"h1": 2}, capabilities={"h1": ["go"]})
    assert facts["hosts"] == [{"name": "h1", "live_runs": 0, "weight": 2, "capabilities": ["go"]}]


def test_dispatch_facts_defaults_weight_and_capabilities_with_no_row():
    facts = dispatch_facts({"max_in_flight": 4}, ["h1"], {})
    assert facts["hosts"] == [{"name": "h1", "live_runs": 0, "weight": 1, "capabilities": []}]


def test_a_ready_tasks_requires_frontmatter_reaches_the_facts():
    def row(task: str, frontmatter: str) -> dict:
        parsed = queue_rows.parse_item("task", ("i", "p1", f"{task}.md"), f"---\n{frontmatter}---\n")
        assert parsed is not None
        return {**parsed, "holder": None, "epoch": 0, "expires_at": None}

    rows = [row("a", "state: ready\nrequires: [go]\n"), row("b", "state: ready\n")]
    facts = gather_facts(replace(_deps(), queue=lambda: rows), NOW)
    assert facts["initiatives"][0]["ready_tasks"] == [
        {"id": "a", "needs": [], "requires": ["go"]}, {"id": "b", "needs": [], "requires": []},
    ]


def test_lease_is_mine_only_for_this_holder_on_a_live_lease():
    def mine(**record: object) -> bool:
        return lease_facts({"epoch": 4, **record}, "s", 7, "h")["mine"]

    assert mine(holder=MINE) is True
    assert mine(holder=lease_holder("t", 8, "g")) is False
    assert mine(holder=MINE, released=True) is False
    assert mine(holder=MINE, stale=True) is False


def test_gather_facts_fills_every_key_from_the_fakes():
    facts = gather_facts(_deps(), NOW)
    # login_hosts is not yet declared on Facts: a later task adds it there once the login watch reads it.
    assert set(facts) == set(Facts.__annotations__) | {"login_hosts"}
    assert facts["dispatch"] == {"max_in_flight": 2, "live_runs": 1, "hosts": []}
    assert facts["initiatives"][0]["landed"] == {"z"}
    assert facts["approved"][0]["phase_done"] is True
    assert facts["intake"] == ["old", "new"]


def test_a_fake_drafts_callable_returning_3_gives_drafts_3():
    assert gather_facts(replace(_deps(), drafts=lambda: 3), NOW)["drafts"] == 3


def test_no_drafts_callable_gives_drafts_0():
    assert gather_facts(_deps(), NOW)["drafts"] == 0


def test_new_missing_repos_drops_paths_already_reported():
    assert new_missing_repos(["/b", "/a", "/a"], {"/b"}) == ["/a"]


def test_a_fake_missing_repos_callable_returning_one_path_gives_facts_holding_it():
    assert gather_facts(replace(_deps(), missing_repos=lambda: ["/x"]), NOW)["missing_repos"] == ["/x"]


def test_absent_missing_and_reported_callables_give_an_empty_list():
    assert gather_facts(_deps(), NOW)["missing_repos"] == []


def test_a_reported_path_leaves_the_facts_empty_and_raises_no_quarantine():
    deps = replace(_deps(), missing_repos=lambda: ["/x"], reported_repos=lambda: {"/x"})
    facts = gather_facts(deps, NOW)
    assert facts["missing_repos"] == []
    assert facts["quarantines"] == gather_facts(_deps(), NOW)["quarantines"]
def test_a_fake_run_exited_callable_appears_under_run_exited():
    assert gather_facts(replace(_deps(), run_exited=lambda: {"i": True}), NOW)["run_exited"] == {"i": True}


def test_no_run_exited_callable_gives_an_empty_mapping():
    assert gather_facts(_deps(), NOW)["run_exited"] == {}


def test_a_fake_lost_runs_callable_appears_under_lost_runs():
    assert gather_facts(replace(_deps(), lost_runs=lambda: {"i": "i-2"}), NOW)["lost_runs"] == {"i": "i-2"}


def test_no_lost_runs_callable_gives_an_empty_mapping():
    assert gather_facts(_deps(), NOW)["lost_runs"] == {}


def test_a_fake_hosts_callables_return_value_appears_verbatim_under_login_hosts():
    rows = [{"name": "h1", "state": "needs_login", "versions_json": "{}"}]
    assert gather_facts(replace(_deps(), hosts=lambda: rows), NOW)["login_hosts"] == rows


def test_an_absent_hosts_callable_gives_an_empty_login_hosts():
    assert gather_facts(_deps(), NOW)["login_hosts"] == []


def _row(initiative: str, task_id: str, state: str, **extra: object) -> dict:
    return {
        "kind": "task", "initiative": initiative, "phase": "p1", "task_id": task_id, "state": state,
        "needs": [], "holder": None, "expires_at": None, "body": "", "extra": {},
        **extra,
    }


ROWS = [
    _row("i", "a", "ready"),
    # A quarantined-state row sits in a different initiative than "a": `route.STATES` has no "quarantined" member,
    # so a quarantined row in the same initiative as a ready task would zero out that initiative's own readiness.
    _row("j", "b", "quarantined", body="the body", extra={"attempts": [{"run": "j-9", "cause": "code", "ts": "2026-09-01"}]}),
    _row("i", "c", "approved"),
    {"kind": "intake", "initiative": "intake", "phase": "", "task_id": "q1", "state": "queued", "extra": {"initiative": None}},
]
STRANDED_RECORDS = [
    {"run": "i-5", "task": "c", "phase": "p1", "initiative": "i", "branch": "i/c", "repo": "r",
     "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"}, "landed": False},
]


def test_rows_drive_ready_intake_quarantined_and_stranded_facts_with_files_ignored():
    deps = replace(
        _deps(),
        queue=lambda: ROWS,
        stranded_records=lambda: STRANDED_RECORDS,
        attempts=lambda: ({"run": "j-9", "phase": "p1", "task": "b", "initiative": "j", "cause": "code"},),
        docket=lambda: {"initiatives": [{"id": "z", "started": False, "ready_tasks": [], "landed": []}], "busy_lanes": 1, "max_in_flight": 2},
        intake=lambda: ["zzz-intake.md"],
        quarantined=lambda: ({"initiative": "z", "phase": "p9", "task": "z"},),
        stranded=lambda: ({"run": "z-1", "task": "z", "phase": "p9", "branch": "z/z", "remedy": None},),
    )
    facts = gather_facts(deps, NOW)
    # "j" also appears (its quarantined-state row leaves it unlaunchable): the fixed point is "i"'s own facts.
    initiative_i = next(i for i in facts["initiatives"] if i["id"] == "i")
    assert initiative_i == {
        "id": "i", "started": True, "ready_tasks": [{"id": "a", "needs": [], "requires": []}], "landed": set(),
    }
    assert facts["intake"] == ["q1.md"]
    assert facts["quarantines"] == [
        {"task_id": "b", "initiative": "j", "cause": "code", "harness_failures": 0, "has_patch": False, "rescue_failed": False},
        {"task_id": "c", "initiative": "i", "cause": "stranded", "harness_failures": 0, "has_patch": False, "rescue_failed": False},
    ]


def test_an_empty_queue_read_falls_back_to_the_file_readers():
    deps = replace(_deps(), queue=lambda: [])
    assert gather_facts(deps, NOW) == gather_facts(_deps(), NOW)


def test_a_claimed_task_is_absent_from_the_ready_facts():
    rows = [
        {**_row("i", "a", "ready"), "holder": "other", "expires_at": "2026-09-26T00:00:00Z"},
        _row("i", "b", "ready"),
    ]
    deps = replace(_deps(), queue=lambda: rows)
    facts = gather_facts(deps, NOW)
    assert [t["id"] for t in facts["initiatives"][0]["ready_tasks"]] == ["b"]
