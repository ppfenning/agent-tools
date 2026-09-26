from dataclasses import replace
from datetime import UTC, datetime, timedelta

from agent_tools import pacing
from agent_tools.chair import lease_holder
from agent_tools.chair_facts import (
    FactsDeps,
    dispatch_facts,
    gather_facts,
    harness_failures,
    lease_facts,
    limits_facts,
)
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_read_attempts import with_stored_rescues
from agent_tools.chair_types import Facts

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
        approved=lambda: [{"id": "a", "initiative": "i", "repo": "r", "phase": "p", "phase_done": True, "needs": [], "run": "i-1"}],
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


def test_weekly_spend_of_85_percent_is_a_hard_stop_with_no_launches():
    limits = gather_facts(_deps(weekly_spent=85.0), NOW)["limits"]
    assert limits == {
        "hard_stop": True,
        "weekly_fraction": 0.85,
        "hard_stop_fraction": 0.85,
        "launch_cap": 0,
        "go_degraded": False,
        "five_hour_fraction": 0.01,
    }


def test_five_hour_fraction_is_the_assessments_spent_fraction():
    assessment = pacing.Assessment(0.42, 0.5, 42.0, 10.0, "go", "deep", "high", None, "on pace")
    assert limits_facts(assessment, POLICY, _window(10.0, 168), 2)["five_hour_fraction"] == 0.42


def test_weekly_spend_under_the_fraction_launches_up_to_max_in_flight():
    limits = gather_facts(_deps(weekly_spent=84.0), NOW)["limits"]
    assert (limits["hard_stop"], limits["launch_cap"]) == (False, 2)


def test_two_harness_attempts_give_harness_failures_of_two():
    attempts = [HARNESS, {**HARNESS, "cause": "code"}, {**HARNESS, "run": "i-2"}, {**HARNESS, "task": "other"}]
    assert harness_failures(attempts, ("i", "p1", "a")) == 2


def test_a_task_id_repeated_in_another_phase_is_counted_apart():
    assert harness_failures([HARNESS, {**HARNESS, "phase": "p2"}], ("i", "p1", "a")) == 1


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
    assert facts["hosts"] == [{"name": "jarvis", "live_runs": 1}]


def test_lease_is_mine_only_for_this_holder_on_a_live_lease():
    def mine(**record: object) -> bool:
        return lease_facts({"epoch": 4, **record}, "s", 7, "h")["mine"]

    assert mine(holder=MINE) is True
    assert mine(holder=lease_holder("t", 8, "g")) is False
    assert mine(holder=MINE, released=True) is False
    assert mine(holder=MINE, stale=True) is False


def test_gather_facts_fills_every_key_from_the_fakes():
    facts = gather_facts(_deps(), NOW)
    assert set(facts) == set(Facts.__annotations__)
    assert facts["dispatch"] == {"max_in_flight": 2, "live_runs": 1, "hosts": []}
    assert facts["initiatives"][0]["landed"] == {"z"}
    assert facts["approved"][0]["phase_done"] is True
    assert facts["intake"] == ["old", "new"]
