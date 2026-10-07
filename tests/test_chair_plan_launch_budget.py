from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_fill import budget_skips, plan_fill
from agent_tools.chair_plan_recover import plan_lost_runs, plan_recover
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
_RECENT = [{"at": "2026-10-07T11:50:00+00:00", "kind": "launch_epic"}, {"at": "2026-10-07T11:40:00+00:00", "kind": "relaunch"}]


def _initiative(id: str, started: bool = True) -> dict:
    return {"id": id, "repo": f"r-{id}", "started": started, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {"a": True, "b": True},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": None,
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _runs(reason_new: str = "main red", reason_old: str = "main red") -> list[dict]:
    return [
        {"run_id": "r2", "quarantined": True, "reason": reason_new, "body": "b1", "main_head": "m1"},
        {"run_id": "r1", "quarantined": True, "reason": reason_old, "body": "b1", "main_head": "m1"},
    ]


def _loop_history(reason_old: str = "main red") -> dict:
    return {"a": {"launches": [], "runs": _runs(reason_old=reason_old), "body": "b1", "main_head": "m1"}}


def _kinds(actions: list[dict], initiative: str) -> list[str]:
    return [a["kind"] for a in actions if a.get("initiative") == initiative]


def test_a_third_launch_epic_in_the_hour_is_refused_and_an_initiative_under_the_limit_still_launches():
    facts = _facts(
        initiatives=[_initiative("a", started=False), _initiative("b", started=False)],
        launch_history={"a": {"launches": _RECENT}, "b": {"launches": _RECENT[:1]}},
        max_launches_per_hour=2,
    )
    assert plan_fill(facts, 3, now=_NOW) == [{"kind": "launch_epic", "initiative": "b"}]
    assert budget_skips(facts, facts["initiatives"], _NOW) == {
        "a": "launch budget: 2 launches in the last hour (max_launches_per_hour=2)"
    }


def test_two_same_reason_quarantines_plan_no_pair_and_one_relaunch_loop_needs_chair():
    facts = _facts(initiatives=[_initiative("a")], launch_history=_loop_history())
    assert plan_recover(facts, _NOW) == [
        {
            "kind": "needs_chair",
            "initiative": "a",
            "cause": "relaunch_loop",
            "reason": "relaunch loop: runs r2 and r1 were both quarantined with 'main red'",
        }
    ]


def test_a_reported_loop_plans_the_refusal_with_no_needs_chair():
    facts = _facts(
        initiatives=[_initiative("a")], launch_history=_loop_history(), relaunch_loop_reported=["r2|r1"]
    )
    assert plan_recover(facts, _NOW) == []


def test_a_changed_reason_allows_the_relaunch():
    facts = _facts(initiatives=[_initiative("a")], launch_history=_loop_history(reason_old="timeout"))
    assert _kinds(plan_recover(facts, _NOW), "a") == ["clear_branches", "relaunch"]


def test_absent_facts_keys_plan_as_today():
    facts = _facts(initiatives=[_initiative("a"), _initiative("b", started=False)])
    assert _kinds(plan_recover(facts, _NOW), "a") == ["clear_branches", "relaunch"]
    assert plan_recover(facts) == plan_recover(facts, _NOW)
    assert plan_fill(facts, 3, now=_NOW) == plan_fill(facts, 3)


def test_without_now_the_budget_is_skipped():
    facts = _facts(initiatives=[_initiative("a")], launch_history=_loop_history())
    assert _kinds(plan_recover(facts), "a") == ["clear_branches", "relaunch"]


def _quarantine(initiative: str, has_patch: bool = False) -> dict:
    return {
        "task_id": f"{initiative}-q",
        "initiative": initiative,
        "cause": "harness",
        "harness_failures": 1,
        "has_patch": has_patch,
        "rescue_failed": False,
    }


def test_an_initiative_over_budget_with_ready_tasks_gets_no_launch_of_any_kind_that_tick():
    facts = _facts(
        initiatives=[_initiative("a"), _initiative("b")],
        launch_history={"a": {"launches": _RECENT}},
        max_launches_per_hour=2,
    )
    actions = plan_tick(facts, _NOW)
    assert _kinds(actions, "a") == []
    assert _kinds(actions, "b") == ["clear_branches", "relaunch"]


def test_an_over_budget_initiative_with_a_harness_quarantine_gets_no_retry_that_tick():
    facts = _facts(
        initiatives=[_initiative("a"), _initiative("b")],
        quarantines=[_quarantine("a"), _quarantine("b")],
        launch_history={"a": {"launches": _RECENT}},
        max_launches_per_hour=2,
    )
    assert _kinds(plan_tick(facts, _NOW), "a") == []
    assert _kinds(plan_tick(facts, _NOW), "b") == ["retry"]
    assert _kinds(plan_tick({k: v for k, v in facts.items() if k != "launch_history"}, _NOW), "a") == ["retry"]


def test_an_over_budget_initiative_with_a_kept_patch_gets_no_rescue_but_without_now_it_does():
    facts = _facts(
        initiatives=[_initiative("a")],
        quarantines=[_quarantine("a", has_patch=True)],
        launch_history={"a": {"launches": _RECENT}},
        max_launches_per_hour=2,
    )
    assert _kinds(plan_recover(facts, _NOW), "a") == []
    assert _kinds(plan_recover(facts), "a") == ["rescue"]


def test_the_budget_does_not_turn_a_chair_report_into_nothing():
    quarantine = {**_quarantine("a"), "cause": "scope"}
    facts = _facts(
        initiatives=[_initiative("a")],
        quarantines=[quarantine],
        launch_history={"a": {"launches": _RECENT}},
        max_launches_per_hour=2,
    )
    assert _kinds(plan_recover(facts, _NOW), "a") == ["needs_chair"]


def test_an_over_budget_lost_run_keeps_its_mark_lost_and_gets_no_relaunch_pair():
    facts = _facts(
        initiatives=[_initiative("a")],
        lost_runs={"a": "a-1"},
        launch_history={"a": {"launches": _RECENT}},
        max_launches_per_hour=2,
    )
    assert _kinds(plan_lost_runs(facts, _NOW), "a") == ["mark_lost"]
    assert _kinds(plan_lost_runs(facts), "a") == ["mark_lost", "clear_branches", "relaunch"]


def test_a_naive_launch_timestamp_counts_as_utc():
    naive = [{"at": "2026-10-07T11:50:00", "kind": "launch_epic"}, {"at": "2026-10-07T11:40:00", "kind": "relaunch"}]
    facts = _facts(
        initiatives=[_initiative("a", started=False)], launch_history={"a": {"launches": naive}}, max_launches_per_hour=2
    )
    assert plan_fill(facts, 3, now=_NOW) == []
    assert budget_skips(facts, facts["initiatives"], _NOW).keys() == {"a"}


def test_an_unparseable_launch_timestamp_is_ignored():
    junk = [{"at": "not a time", "kind": "launch_epic"}, {"kind": "relaunch"}]
    facts = _facts(initiatives=[_initiative("a", started=False)], launch_history={"a": {"launches": junk}})
    assert plan_fill(facts, 3, now=_NOW) == [{"kind": "launch_epic", "initiative": "a"}]


def test_a_loop_refused_relaunch_is_not_launched_by_fill_and_reports_once():
    facts = _facts(initiatives=[_initiative("a")], launch_history=_loop_history())
    actions = plan_tick(facts, _NOW)
    assert _kinds(actions, "a") == ["needs_chair"]
    reported = plan_tick({**facts, "relaunch_loop_reported": ["r2|r1"]}, _NOW)
    assert _kinds(reported, "a") == []
