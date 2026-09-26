import copy

from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_types import Facts, InitiativeFacts, QuarantineFacts


def _initiative(started: bool = True, ready: list[dict] | None = None, landed: set[str] | None = None) -> InitiativeFacts:
    return {
        "id": "i",
        "started": started,
        "ready_tasks": [{"id": "t1", "needs": ["a"]}] if ready is None else ready,  # type: ignore[typeddict-item]
        "landed": {"a"} if landed is None else landed,
    }


def _quarantine(
    cause: str = "harness",
    failures: int = 1,
    initiative: str = "i",
    task_id: str = "q1",
    has_patch: bool = False,
    rescue_failed: bool = False,
) -> QuarantineFacts:
    return {
        "task_id": task_id,
        "initiative": initiative,
        "cause": cause,
        "harness_failures": failures,
        "has_patch": has_patch,
        "rescue_failed": rescue_failed,
    }


def _facts(initiatives: list[InitiativeFacts], quarantines: list[QuarantineFacts]) -> Facts:
    return {
        "lease": {"holder": "a", "host": "h", "epoch": 1, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        "dispatch": {"max_in_flight": 3, "live_runs": 0},
        "approved": [],
        "initiatives": initiatives,
        "quarantines": quarantines,
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
    }


def test_a_started_initiative_with_all_needs_landed_is_cleared_then_relaunched():
    assert plan_recover(_facts([_initiative()], [])) == [
        {"kind": "clear_branches", "initiative": "i"},
        {"kind": "relaunch", "initiative": "i"},
    ]


def test_a_harness_quarantine_with_one_failure_gets_one_retry():
    assert plan_recover(_facts([], [_quarantine()])) == [{"kind": "retry", "task_id": "q1", "initiative": "i"}]


def test_a_harness_quarantine_with_a_patch_gets_one_rescue():
    assert plan_recover(_facts([], [_quarantine(has_patch=True)])) == [
        {"kind": "rescue", "initiative": "i", "task_id": "q1"}
    ]


def test_a_failed_rescue_needs_the_chair_and_gets_no_rescue_or_retry():
    facts = _facts([], [_quarantine(has_patch=True, rescue_failed=True)])
    assert plan_recover(facts) == [{"kind": "needs_chair", "initiative": "i", "cause": "harness"}]


def test_two_harness_failures_with_a_patch_are_not_rescued():
    facts = _facts([], [_quarantine(failures=2, has_patch=True)])
    assert plan_recover(facts) == [{"kind": "needs_chair", "initiative": "i", "cause": "harness"}]


def test_a_non_harness_cause_with_a_patch_is_not_rescued():
    facts = _facts([], [_quarantine(cause="verify", has_patch=True)])
    assert plan_recover(facts) == [{"kind": "needs_chair", "initiative": "i", "cause": "verify"}]


def test_a_rescued_initiative_is_not_relaunched_this_tick():
    facts = _facts([_initiative()], [_quarantine(has_patch=True)])
    assert plan_recover(facts) == [{"kind": "rescue", "initiative": "i", "task_id": "q1"}]


def test_a_non_harness_cause_needs_the_chair_and_blocks_relaunch():
    facts = _facts([_initiative()], [_quarantine(cause="verify")])
    assert plan_recover(facts) == [{"kind": "needs_chair", "initiative": "i", "cause": "verify"}]


def test_two_harness_failures_need_the_chair_and_block_relaunch():
    facts = _facts([_initiative()], [_quarantine(failures=2)])
    assert plan_recover(facts) == [{"kind": "needs_chair", "initiative": "i", "cause": "harness"}]


def test_an_initiative_that_is_not_started_is_never_relaunched():
    assert plan_recover(_facts([_initiative(started=False)], [])) == []


def test_an_unlanded_need_blocks_relaunch():
    assert plan_recover(_facts([_initiative(landed=set())], [])) == []


def test_an_initiative_with_no_ready_tasks_is_not_relaunched():
    assert plan_recover(_facts([_initiative(ready=[])], [])) == []


def test_a_retried_initiative_is_not_relaunched_this_tick():
    facts = _facts([_initiative()], [_quarantine()])
    assert plan_recover(facts) == [{"kind": "retry", "task_id": "q1", "initiative": "i"}]


def test_a_needs_chair_quarantine_drops_a_retry_on_the_same_initiative():
    facts = _facts([], [_quarantine(task_id="q1"), _quarantine(cause="verify", task_id="q2")])
    assert plan_recover(facts) == [{"kind": "needs_chair", "initiative": "i", "cause": "verify"}]


def test_plan_recover_leaves_the_facts_unchanged():
    facts = _facts([_initiative()], [_quarantine(cause="verify", initiative="j")])
    before = copy.deepcopy(facts)
    plan_recover(facts)
    assert facts == before
