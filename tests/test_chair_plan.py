import copy

from agent_tools.chair_plan import _free_lanes, plan_tick
from agent_tools.chair_types import Facts

_SCOPE_QUARANTINE = {"task_id": "p", "initiative": "m", "cause": "scope", "harness_failures": 0}


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _lease(**fields) -> dict:
    return {"holder": "other", "host": "elsewhere", "epoch": 7, "mine": False, "released": False, "stale": False, **fields}


def _initiative(id: str) -> dict:
    return {"id": id, "started": True, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}


def _blocked(id: str) -> dict:
    """Started with a ready task whose need has not landed: it can launch an epic but not relaunch."""
    return {"id": id, "started": True, "ready_tasks": [{"id": f"{id}-t", "needs": ["z"]}], "landed": set()}


def _approved(id: str) -> dict:
    return {"id": id, "initiative": "x", "repo": "r", "phase_done": True, "needs": []}


def _kinds(actions: list[dict]) -> list[str]:
    return [a["kind"] for a in actions]


def test_a_lease_held_by_another_returns_only_standby_with_holder_and_host():
    facts = _facts(lease=_lease(), approved=[_approved("t1")], initiatives=[_initiative("i")])
    assert plan_tick(facts) == [{"kind": "standby", "holder": "other", "host": "elsewhere", "epoch": 7}]


def test_a_released_lease_returns_only_take_lease():
    assert plan_tick(_facts(lease=_lease(released=True), approved=[_approved("t1")])) == [{"kind": "take_lease", "epoch": 7}]


def test_a_stale_lease_returns_only_take_lease():
    assert plan_tick(_facts(lease=_lease(stale=True), initiatives=[_initiative("i")])) == [{"kind": "take_lease", "epoch": 7}]


def test_a_hard_stop_returns_lands_and_needs_chair_and_no_launches():
    facts = _facts(
        limits={"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        approved=[_approved("t1")],
        initiatives=[_initiative("i"), _initiative("j")],
        quarantines=[
            {"task_id": "q", "initiative": "k", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False},
            {"task_id": "p", "initiative": "m", "cause": "scope", "harness_failures": 0},
        ],
        intake=["n1", "n2"],
    )
    assert plan_tick(facts) == [
        {"kind": "land", "task_id": "t1", "repo": "r", "initiative": "x", "epoch": 7},
        {"kind": "needs_chair", "initiative": "m", "cause": "scope", "epoch": 7},
    ]


def test_go_degraded_blocks_every_launch_but_keeps_lands_and_needs_chair():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": True},
        approved=[_approved("t1")],
        initiatives=[_initiative("i")],
        quarantines=[{"task_id": "p", "initiative": "m", "cause": "scope", "harness_failures": 0}],
        intake=["n1", "n2"],
    )
    assert _kinds(plan_tick(facts)) == ["land", "needs_chair"]


def test_launch_cap_keeps_the_first_relaunches_in_order_and_drops_the_paired_clear_of_the_rest():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        initiatives=[_initiative("a"), _initiative("b"), _initiative("c")],
    )
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "clear_branches", "initiative": "b", "epoch": 7},
        {"kind": "relaunch", "initiative": "b", "epoch": 7},
    ]


def test_a_harness_retry_counts_against_the_launch_cap():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 1, "go_degraded": False},
        quarantines=[
            {"task_id": "q1", "initiative": "a", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False},
            {"task_id": "q2", "initiative": "b", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False},
        ],
    )
    assert plan_tick(facts) == [{"kind": "retry", "task_id": "q1", "initiative": "a", "epoch": 7}]


_RESCUE = {"kind": "rescue", "task_id": "r1", "initiative": "a"}
_RETRY = {"kind": "retry", "task_id": "q1", "initiative": "b"}
_NEEDS_CHAIR_BARE = {"kind": "needs_chair", "initiative": "m", "cause": "scope"}


def _recovering(monkeypatch, actions: list[dict]) -> None:
    monkeypatch.setattr("agent_tools.chair_plan.plan_recover", lambda facts: actions)


def test_a_rescue_counts_against_the_launch_cap(monkeypatch):
    _recovering(monkeypatch, [_RESCUE, _RETRY])
    facts = _facts(limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 1, "go_degraded": False})
    assert plan_tick(facts) == [{**_RESCUE, "epoch": 7}]


def test_a_rescue_is_dropped_at_the_hard_stop_while_needs_chair_passes(monkeypatch):
    _recovering(monkeypatch, [_RESCUE, _NEEDS_CHAIR_BARE])
    facts = _facts(limits={"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False})
    assert plan_tick(facts) == [{**_NEEDS_CHAIR_BARE, "epoch": 7}]


def test_a_kept_rescue_is_stamped_with_the_lease_epoch(monkeypatch):
    _recovering(monkeypatch, [_RESCUE])
    facts = _facts(lease={"holder": "a", "host": "h", "epoch": 42, "mine": True, "released": False, "stale": False})
    assert plan_tick(facts) == [{**_RESCUE, "epoch": 42}]


def test_free_lanes_is_the_launch_cap_minus_kept_launches_when_the_cap_binds():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 3, "go_degraded": False},
        dispatch={"max_in_flight": 9, "live_runs": 0},
        initiatives=[_initiative("a"), _blocked("b"), _blocked("c"), _blocked("d")],
    )
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "launch_epic", "initiative": "b", "epoch": 7},
        {"kind": "launch_epic", "initiative": "c", "epoch": 7},
    ]


def test_a_relaunched_initiative_is_not_also_launched_as_an_epic():
    facts = _facts(initiatives=[_initiative("a"), _initiative("b"), _blocked("c")])
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "clear_branches", "initiative": "b", "epoch": 7},
        {"kind": "relaunch", "initiative": "b", "epoch": 7},
        {"kind": "launch_epic", "initiative": "c", "epoch": 7},
    ]


def test_free_lanes_is_max_in_flight_minus_live_minus_kept_when_dispatch_binds():
    facts = _facts(
        dispatch={"max_in_flight": 3, "live_runs": 1},
        initiatives=[_blocked("a"), _blocked("b"), _blocked("c")],
    )
    assert _kinds(plan_tick(facts)) == ["launch_epic", "launch_epic"]


def test_dispatch_lanes_are_counted_after_the_kept_launches():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        dispatch={"max_in_flight": 3, "live_runs": 0},
        initiatives=[_initiative("a"), _blocked("b"), _blocked("c"), _blocked("d")],
    )
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "launch_epic", "initiative": "b", "epoch": 7},
        {"kind": "launch_epic", "initiative": "c", "epoch": 7},
    ]


def test_free_lanes_is_the_smaller_of_the_cap_and_dispatch_room_after_kept_launches():
    assert _free_lanes(5, 1, {"max_in_flight": 3, "live_runs": 0}) == 2
    assert _free_lanes(2, 1, {"max_in_flight": 9, "live_runs": 0}) == 1


def test_free_lanes_floors_at_zero_when_live_runs_exceed_max_in_flight():
    assert _free_lanes(5, 0, {"max_in_flight": 2, "live_runs": 4}) == 0


def test_a_negative_launch_cap_launches_nothing():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": -1, "go_degraded": False},
        initiatives=[_initiative("a"), _initiative("b")],
    )
    assert plan_tick(facts) == []


def test_a_quarantined_initiative_with_a_needs_chair_is_not_also_launched_as_an_epic():
    facts = _facts(initiatives=[_blocked("m"), _blocked("b")], quarantines=[_SCOPE_QUARANTINE])
    assert plan_tick(facts) == [
        {"kind": "needs_chair", "initiative": "m", "cause": "scope", "epoch": 7},
        {"kind": "launch_epic", "initiative": "b", "epoch": 7},
    ]


def test_a_retried_initiative_is_not_also_launched_as_an_epic():
    retry = {"task_id": "q1", "initiative": "a", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False}
    facts = _facts(initiatives=[_blocked("a"), _blocked("b")], quarantines=[retry])
    assert plan_tick(facts) == [
        {"kind": "retry", "task_id": "q1", "initiative": "a", "epoch": 7},
        {"kind": "launch_epic", "initiative": "b", "epoch": 7},
    ]


_NEEDS_CHAIR = [{"kind": "needs_chair", "initiative": "m", "cause": "scope", "epoch": 7}]


def test_needs_chair_passes_through_at_the_hard_stop():
    facts = _facts(
        limits={"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        quarantines=[_SCOPE_QUARANTINE],
    )
    assert plan_tick(facts) == _NEEDS_CHAIR


def test_needs_chair_passes_through_under_go_degraded():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": True},
        quarantines=[_SCOPE_QUARANTINE],
    )
    assert plan_tick(facts) == _NEEDS_CHAIR


def _mixed() -> Facts:
    return _facts(
        lease={"holder": "a", "host": "h", "epoch": 42, "mine": True, "released": False, "stale": False},
        approved=[_approved("t1")],
        initiatives=[_initiative("i")],
        quarantines=[_SCOPE_QUARANTINE],
        intake=["n1", "n2"],
    )


def test_every_action_carries_the_lease_epoch():
    assert [(a["kind"], a["epoch"]) for a in plan_tick(_mixed())] == [
        ("land", 42),
        ("needs_chair", 42),
        ("clear_branches", 42),
        ("relaunch", 42),
        ("launch_decompose", 42),
        ("launch_decompose", 42),
    ]


def test_plan_tick_leaves_the_facts_alone():
    facts = _mixed()
    before = copy.deepcopy(facts)
    plan_tick(facts)
    assert facts == before
