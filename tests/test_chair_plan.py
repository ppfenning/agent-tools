import copy
from datetime import UTC, datetime

from agent_tools.chair_plan import _free_lanes, _launch_cap, initiative_homes, plan_stall, plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

_SCOPE_QUARANTINE = {"task_id": "p", "initiative": "m", "cause": "scope", "harness_failures": 0}


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
        "run_exited": {},
        "lost_runs": {},
        "last_housekeeping_at": None,
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
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
    return {
        "id": id, "initiative": "x", "repo": "r", "phase": "p", "phase_done": True, "needs": [], "run": "x-1",
        "needs_fetch": False,
    }


def _kinds(actions: list[dict]) -> list[str]:
    return [a["kind"] for a in actions]


def test_a_lease_held_by_another_returns_only_standby_with_holder_and_host():
    facts = _facts(lease=_lease(), approved=[_approved("t1")], initiatives=[_initiative("i")])
    assert plan_tick(facts) == [{"kind": "standby", "holder": "other", "host": "elsewhere", "epoch": 7}]


def test_a_released_lease_returns_only_take_lease():
    assert plan_tick(_facts(lease=_lease(released=True), approved=[_approved("t1")])) == [{"kind": "take_lease", "epoch": 7}]


def test_a_stale_lease_returns_only_take_lease():
    assert plan_tick(_facts(lease=_lease(stale=True), initiatives=[_initiative("i")])) == [{"kind": "take_lease", "epoch": 7}]


def test_a_quarantined_unstarted_initiative_launches_no_epic_and_the_lane_goes_to_another():
    quarantined = {"id": "q", "started": False, "ready_tasks": [{"id": "q-t", "needs": []}], "landed": set()}
    other = {**_initiative("o"), "started": False}
    facts = _facts(
        initiatives=[quarantined, other],
        quarantines=[{"task_id": "p", "initiative": "q", "cause": "scope", "harness_failures": 0}],
    )
    assert [(a["kind"], a["initiative"]) for a in plan_tick(facts) if a["kind"] == "launch_epic"] == [("launch_epic", "o")]


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
        {"kind": "land_phase", "initiative": "x", "phase": "p", "repo": "r", "run": "x-1", "epoch": 7},
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
    assert _kinds(plan_tick(facts)) == ["land_phase", "needs_chair"]


def test_launch_cap_keeps_the_first_relaunches_in_order_and_drops_the_paired_clear_of_the_rest():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        initiatives=[_initiative("a"), _initiative("b"), _initiative("c")],
        run_exited={"a": True, "b": True, "c": True},
    )
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "clear_branches", "initiative": "b", "epoch": 7},
        {"kind": "relaunch", "initiative": "b", "epoch": 7},
    ]


def test_a_relaunch_for_an_initiative_with_a_partial_phase_carries_it_forward():
    approved = {"id": "a-p-t", "initiative": "a", "repo": "r", "phase": "p", "phase_done": False, "needs": [], "run": "a-1", "needs_fetch": False}
    facts = _facts(initiatives=[_initiative("a")], run_exited={"a": True}, approved=[approved])
    assert [a for a in plan_tick(facts) if a["kind"] == "clear_branches"] == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7, "carry": ["p"]}
    ]


def test_a_relaunch_for_an_initiative_with_no_partial_phase_carries_nothing():
    facts = _facts(initiatives=[_initiative("a")], run_exited={"a": True})
    assert [a for a in plan_tick(facts) if a["kind"] == "clear_branches"] == [{"kind": "clear_branches", "initiative": "a", "epoch": 7}]


def test_relaunches_stop_at_the_free_lanes_of_this_machine():
    facts = _facts(
        dispatch={"max_in_flight": 2, "live_runs": 1, "hosts": []},
        initiatives=[_initiative("a"), _initiative("b")],
        run_exited={"a": True, "b": True},
    )
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
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


_STALE = {"kind": "stale_to_draft", "initiative": "z", "stale_tasks": ["z-1"], "reason": "no file change, run, or chair action in 26 days", "since": "2026-09-27T12:00:00+00:00"}


def _staling(monkeypatch, actions: list[dict]) -> None:
    monkeypatch.setattr("agent_tools.chair_plan.plan_stale", lambda facts, now: actions)


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


def test_a_stale_to_draft_passes_through_uncounted_while_a_retry_is_capped_normally(monkeypatch):
    _recovering(monkeypatch, [_RETRY])
    _staling(monkeypatch, [_STALE])
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 1, "go_degraded": False},
        last_housekeeping_at="2026-09-27T11:00:00+00:00",
    )
    assert plan_tick(facts, _NOW) == [{**_STALE, "epoch": 7}, {**_RETRY, "epoch": 7}]


def test_a_stale_to_draft_is_kept_at_the_hard_stop_alongside_needs_chair(monkeypatch):
    _recovering(monkeypatch, [{"kind": "relaunch", "initiative": "a"}, _RETRY, _NEEDS_CHAIR_BARE])
    _staling(monkeypatch, [_STALE])
    facts = _facts(
        limits={"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        last_housekeeping_at="2026-09-27T11:00:00+00:00",
    )
    assert plan_tick(facts, _NOW) == [{**_STALE, "epoch": 7}, {**_NEEDS_CHAIR_BARE, "epoch": 7}]


def _hosted_dispatch() -> dict:
    return {"max_in_flight": 1, "live_runs": 1, "hosts": [{"name": "jarvis", "live_runs": 0}]}


def test_a_relaunch_past_the_local_cap_carries_the_free_lane_hosts_name(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    facts = _facts(dispatch=_hosted_dispatch(), run_exited={"a": True})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "host": "jarvis", "epoch": 7},
    ]


def test_only_one_of_two_overflow_launches_gets_the_single_free_host_lane(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}, _RETRY])
    facts = _facts(dispatch=_hosted_dispatch(), run_exited={"a": True})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "host": "jarvis", "epoch": 7},
    ]


def test_a_rescue_past_the_cap_is_dropped_even_with_a_free_lane_host(monkeypatch):
    _recovering(monkeypatch, [_RESCUE])
    facts = _facts(dispatch=_hosted_dispatch())
    assert plan_tick(facts) == []


def test_a_home_with_a_free_lane_gets_the_relaunch_directly(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    # friday outweighs jarvis, so _place_on_hosts ranking would pick friday; home names jarvis instead.
    dispatch = {
        "max_in_flight": 1,
        "live_runs": 1,
        "hosts": [{"name": "jarvis", "live_runs": 0, "weight": 1}, {"name": "friday", "live_runs": 0, "weight": 10}],
    }
    facts = _facts(dispatch=dispatch, run_exited={"a": True}, home={"a": "jarvis"})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "host": "jarvis", "epoch": 7},
    ]


def test_a_home_with_no_free_lane_plans_nothing_for_it_while_a_homeless_relaunch_takes_the_free_lane_elsewhere(monkeypatch):
    _recovering(
        monkeypatch,
        [
            {"kind": "clear_branches", "initiative": "a"},
            {"kind": "relaunch", "initiative": "a"},
            {"kind": "clear_branches", "initiative": "b"},
            {"kind": "relaunch", "initiative": "b"},
        ],
    )
    dispatch = {
        "max_in_flight": 1,
        "live_runs": 1,
        "hosts": [{"name": "jarvis", "live_runs": 1, "capacity": 1}, {"name": "friday", "live_runs": 0}],
    }
    facts = _facts(dispatch=dispatch, run_exited={"a": True, "b": True}, home={"a": "jarvis"})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "b", "epoch": 7},
        {"kind": "relaunch", "initiative": "b", "host": "friday", "epoch": 7},
    ]


def test_a_homeless_initiative_places_as_before(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    facts = _facts(dispatch=_hosted_dispatch(), run_exited={"a": True}, home={"z": "elsewhere"})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "host": "jarvis", "epoch": 7},
    ]


def _home_approved(run: str) -> dict:
    return {"id": "a-t", "initiative": "a", "repo": "r", "phase": "p", "phase_done": False, "needs": [], "run": run, "needs_fetch": False}


def _two_host_dispatch() -> dict:
    return {
        "max_in_flight": 1,
        "live_runs": 1,
        "hosts": [{"name": "jarvis", "live_runs": 0}, {"name": "friday", "live_runs": 0}],
    }


def test_a_home_with_a_lapsed_login_moves_the_relaunch_with_a_fetch_planned_first(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    facts = _facts(
        dispatch=_two_host_dispatch(),
        run_exited={"a": True},
        home={"a": "jarvis"},
        approved=[_home_approved("a-3")],
        login_hosts=[_login_host("jarvis", login_ok=False, checked_at=_NOW.isoformat())],
    )
    assert plan_tick(facts) == [
        {"kind": "fetch", "run": "a-3", "repo": "r", "initiative": "a", "epoch": 7},
        {"kind": "clear_branches", "initiative": "a", "carry": ["p"], "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "host": "friday", "epoch": 7},
        {"kind": "needs_chair", "host": "jarvis", "cause": "login_lapsed", "epoch": 7},
    ]


def test_a_home_with_a_lost_lane_moves_the_relaunch():
    facts = _facts(
        dispatch=_two_host_dispatch(),
        run_exited={"a": True},
        home={"a": "jarvis"},
        approved=[_home_approved("a-3")],
        lost_runs={"a": "a-3"},
    )
    assert plan_tick(facts) == [
        {"kind": "fetch", "run": "a-3", "repo": "r", "initiative": "a", "epoch": 7},
        {"kind": "mark_lost", "initiative": "a", "run": "a-3", "epoch": 7},
        {"kind": "clear_branches", "initiative": "a", "carry": ["p"], "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "host": "friday", "epoch": 7},
    ]


def test_a_moved_home_fetches_its_newest_run_not_the_first_approved_row(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    older = {**_home_approved("a-2"), "id": "a-old", "phase": "q", "repo": "old"}
    facts = _facts(
        dispatch=_two_host_dispatch(),
        run_exited={"a": True},
        home={"a": "jarvis"},
        approved=[older, _home_approved("a-10")],
        login_hosts=[_login_host("jarvis", login_ok=False, checked_at=_NOW.isoformat())],
    )
    assert plan_tick(facts)[0] == {"kind": "fetch", "run": "a-10", "repo": "r", "initiative": "a", "epoch": 7}


def test_a_stuck_home_with_no_run_to_fetch_does_not_move_and_reaches_the_chair(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    facts = _facts(
        dispatch=_two_host_dispatch(),
        run_exited={"a": True},
        home={"a": "jarvis"},
        approved=[_home_approved("")],
        login_hosts=[_login_host("jarvis", login_ok=False, checked_at=_NOW.isoformat())],
    )
    assert plan_tick(facts) == [
        {"kind": "needs_chair", "initiative": "a", "cause": "home_unfetchable", "epoch": 7},
        {"kind": "needs_chair", "host": "jarvis", "cause": "login_lapsed", "epoch": 7},
    ]


def test_a_busy_but_live_home_does_not_move(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    dispatch = {
        "max_in_flight": 1,
        "live_runs": 1,
        "hosts": [{"name": "jarvis", "live_runs": 1, "capacity": 1}, {"name": "friday", "live_runs": 0}],
    }
    facts = _facts(
        dispatch=dispatch,
        run_exited={"a": True},
        home={"a": "jarvis"},
        approved=[_home_approved("a-3")],
        login_hosts=[_login_host("jarvis")],
    )
    assert plan_tick(facts) == []


_STALLED_LAST_CALL = {"role": "builder", "task": "t1", "ts": "2026-09-27T11:29:00Z"}
_FRESH_LAST_CALL = {"role": "builder", "task": "t1", "ts": "2026-09-27T11:31:00Z"}
_STALL_REASON = "builder t1, idle 31m"


def _stall_candidate(**overrides) -> dict:
    return {
        "run": "r1",
        "initiative": "i",
        "local": True,
        "started_at": "2026-09-27T11:00:00Z",
        "last_call": _STALLED_LAST_CALL,
        "usr1_sent": False,
        **overrides,
    }


def test_a_local_candidate_not_yet_signalled_plans_one_stalled_usr1():
    assert plan_stall([_stall_candidate()], _NOW) == [{"kind": "stalled_usr1", "run": "r1", "initiative": "i"}]


def test_a_local_candidate_already_signalled_plans_stalled_stop_and_needs_chair():
    assert plan_stall([_stall_candidate(usr1_sent=True)], _NOW) == [
        {"kind": "stalled_stop", "run": "r1", "initiative": "i"},
        {"kind": "needs_chair", "initiative": "i", "run": "r1", "cause": "stalled", "reason": _STALL_REASON},
    ]


def test_a_local_candidate_idle_under_the_threshold_plans_nothing():
    assert plan_stall([_stall_candidate(last_call=_FRESH_LAST_CALL)], _NOW) == []


def test_a_remote_candidate_plans_only_needs_chair_regardless_of_usr1_sent():
    remote = _stall_candidate(local=False, usr1_sent=True)
    assert plan_stall([remote], _NOW) == [
        {"kind": "needs_chair", "initiative": "i", "run": "r1", "cause": "stalled", "reason": _STALL_REASON},
    ]


def test_a_stalled_run_passes_through_plan_tick_uncounted_at_the_hard_stop():
    facts = _facts(
        limits={"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 0, "go_degraded": False},
        stall_candidates=[_stall_candidate()],
        last_housekeeping_at="2026-09-27T11:00:00+00:00",
    )
    assert plan_tick(facts, _NOW) == [{"kind": "stalled_usr1", "run": "r1", "initiative": "i", "epoch": 7}]


def test_free_lanes_is_the_launch_cap_minus_kept_launches_when_the_cap_binds():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 3, "go_degraded": False},
        dispatch={"max_in_flight": 9, "live_runs": 0, "hosts": []},
        initiatives=[_initiative("a"), _blocked("b"), _blocked("c"), _blocked("d")],
        run_exited={"a": True},
    )
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "launch_epic", "initiative": "b", "epoch": 7},
        {"kind": "launch_epic", "initiative": "c", "epoch": 7},
    ]


def test_a_relaunched_initiative_is_not_also_launched_as_an_epic():
    facts = _facts(initiatives=[_initiative("a"), _initiative("b"), _blocked("c")], run_exited={"a": True, "b": True})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "clear_branches", "initiative": "b", "epoch": 7},
        {"kind": "relaunch", "initiative": "b", "epoch": 7},
        {"kind": "launch_epic", "initiative": "c", "epoch": 7},
    ]


def test_free_lanes_is_max_in_flight_minus_live_minus_kept_when_dispatch_binds():
    facts = _facts(
        dispatch={"max_in_flight": 3, "live_runs": 1, "hosts": []},
        initiatives=[_blocked("a"), _blocked("b"), _blocked("c")],
    )
    assert _kinds(plan_tick(facts)) == ["launch_epic", "launch_epic"]


def test_dispatch_lanes_are_counted_after_the_kept_launches():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        dispatch={"max_in_flight": 3, "live_runs": 0, "hosts": []},
        initiatives=[_initiative("a"), _blocked("b"), _blocked("c"), _blocked("d")],
        run_exited={"a": True},
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


def test_launch_cap_is_zero_when_a_smoke_hold_is_present():
    limits = {
        "hard_stop": False,
        "weekly_fraction": 0.1,
        "hard_stop_fraction": 0.9,
        "launch_cap": 3,
        "go_degraded": False,
        "smoke_hold": {
            "land": {"repo": "r", "pr": 1, "commit": "abc123"},
            "failing_command": ["pytest", "-q"],
            "tail": "AssertionError: boom",
            "cause": "smoke_failed",
        },
    }
    assert _launch_cap(limits) == 0


def test_launch_cap_is_unaffected_when_smoke_hold_is_none():
    limits = {
        "hard_stop": False,
        "weekly_fraction": 0.1,
        "hard_stop_fraction": 0.9,
        "launch_cap": 3,
        "go_degraded": False,
        "smoke_hold": None,
    }
    assert _launch_cap(limits) == 3


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
        run_exited={"i": True},
    )


def test_every_action_carries_the_lease_epoch():
    assert [(a["kind"], a["epoch"]) for a in plan_tick(_mixed())] == [
        ("land_phase", 42),
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


def test_another_holder_with_an_expired_takeover_plans_take_lease_with_the_reason():
    facts = _facts(lease=_lease(expired=True, until="2026-09-26T15:00:00+00:00"), approved=[_approved("t1")])
    assert plan_tick(facts) == [{"kind": "take_lease", "reason": "takeover expired at 2026-09-26T15:00:00+00:00", "epoch": 7}]


def _login_host(name: str, login_ok: bool | None = None, checked_at: str | None = None) -> dict:
    versions: dict = {}
    if login_ok is not None:
        versions["login_ok"] = login_ok
    if checked_at is not None:
        versions["login_checked_at"] = checked_at
    return {"name": name, "state": "active", "versions_json": versions}


def test_needs_chair_login_lapsed_passes_through_at_the_hard_stop():
    facts = _facts(
        limits={"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        login_hosts=[_login_host("jarvis", login_ok=False, checked_at=_NOW.isoformat())],
    )
    assert plan_tick(facts) == [{"kind": "needs_chair", "host": "jarvis", "cause": "login_lapsed", "epoch": 7}]


def test_a_due_host_yields_one_check_login_action():
    facts = _facts(login_hosts=[_login_host("jarvis")], last_housekeeping_at=_NOW.isoformat())
    assert plan_tick(facts, _NOW) == [{"kind": "check_login", "host": "jarvis", "epoch": 7}]


def test_a_host_neither_due_nor_blocked_yields_no_login_actions():
    facts = _facts(
        login_hosts=[_login_host("jarvis", login_ok=True, checked_at=_NOW.isoformat())],
        last_housekeeping_at=_NOW.isoformat(),
    )
    assert plan_tick(facts, _NOW) == []


def test_another_holder_inside_its_takeover_plans_standby_naming_until():
    facts = _facts(lease=_lease(expired=False, until="2026-09-26T15:00:00+00:00"))
    assert plan_tick(facts) == [{"kind": "standby", "holder": "other", "host": "elsewhere", "until": "2026-09-26T15:00:00+00:00", "epoch": 7}]


def test_a_remote_unfetched_initiative_gets_a_fetch_exit_action_before_its_relaunch():
    facts = _facts(initiatives=[_initiative("i")], remote_unfetched={"i": "i-run-1"}, run_exited={"i": True})
    assert plan_tick(facts) == [{"kind": "fetch_exit", "initiative": "i", "run": "i-run-1", "epoch": 7}]


def test_a_remote_unfetched_initiative_plans_no_relaunch_even_with_ready_tasks_and_met_needs():
    facts = _facts(initiatives=[_initiative("a")], remote_unfetched={"a": "a-run-1"}, run_exited={"a": True})
    assert "relaunch" not in _kinds(plan_tick(facts))
    assert "clear_branches" not in _kinds(plan_tick(facts))


def test_a_remote_unfetched_initiative_plans_no_launch_epic_even_with_ready_tasks_and_met_needs():
    unstarted = {**_initiative("o"), "started": False}
    facts = _facts(initiatives=[unstarted], remote_unfetched={"o": "o-run-1"})
    assert plan_tick(facts) == [{"kind": "fetch_exit", "initiative": "o", "run": "o-run-1", "epoch": 7}]


def test_an_initiative_absent_from_remote_unfetched_still_relaunches_as_today():
    facts = _facts(initiatives=[_initiative("a"), _initiative("b")], remote_unfetched={"a": "a-run-1"}, run_exited={"b": True})
    assert plan_tick(facts) == [
        {"kind": "fetch_exit", "initiative": "a", "run": "a-run-1", "epoch": 7},
        {"kind": "clear_branches", "initiative": "b", "epoch": 7},
        {"kind": "relaunch", "initiative": "b", "epoch": 7},
    ]


def test_an_initiative_with_a_recorded_exit_relaunches_as_today():
    facts = _facts(initiatives=[_initiative("i")], run_exited={"i": True})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "i", "epoch": 7},
        {"kind": "relaunch", "initiative": "i", "epoch": 7},
    ]


def test_an_initiative_without_a_recorded_exit_plans_no_relaunch():
    """The same ready, needs-met shape that used to relaunch on a stale-looking lease; run_exited false blocks it."""
    facts = _facts(initiatives=[_initiative("i")], run_exited={"i": False})
    assert plan_tick(facts) == []


def test_an_initiative_absent_from_run_exited_plans_no_relaunch():
    facts = _facts(initiatives=[_initiative("i")])
    assert plan_tick(facts) == []


def test_a_lost_run_initiative_gets_mark_lost_then_clear_branches_then_relaunch_with_no_host():
    facts = _facts(
        initiatives=[_initiative("i")],
        lost_runs={"i": "i-run-1"},
        dispatch={"max_in_flight": 5, "live_runs": 0, "hosts": []},
    )
    assert plan_tick(facts) == [
        {"kind": "mark_lost", "initiative": "i", "run": "i-run-1", "epoch": 7},
        {"kind": "clear_branches", "initiative": "i", "epoch": 7},
        {"kind": "relaunch", "initiative": "i", "epoch": 7},
    ]


def test_a_lost_run_relaunches_even_with_run_exited_false():
    facts = _facts(
        initiatives=[_initiative("i")],
        lost_runs={"i": "i-run-1"},
        run_exited={"i": False},
        dispatch={"max_in_flight": 5, "live_runs": 0, "hosts": []},
    )
    assert plan_tick(facts) == [
        {"kind": "mark_lost", "initiative": "i", "run": "i-run-1", "epoch": 7},
        {"kind": "clear_branches", "initiative": "i", "epoch": 7},
        {"kind": "relaunch", "initiative": "i", "epoch": 7},
    ]


def test_an_initiative_absent_from_lost_runs_still_gates_on_run_exited():
    facts = _facts(initiatives=[_initiative("a"), _initiative("b")], run_exited={"a": True, "b": False})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
    ]


def test_a_lost_run_initiative_with_an_unrelated_quarantine_still_gets_needs_chair():
    facts = _facts(
        initiatives=[_initiative("i")],
        lost_runs={"i": "i-run-1"},
        quarantines=[{"task_id": "q1", "initiative": "i", "cause": "code", "harness_failures": 0}],
    )
    assert plan_tick(facts) == [
        {"kind": "needs_chair", "initiative": "i", "cause": "code", "epoch": 7},
        {"kind": "mark_lost", "initiative": "i", "run": "i-run-1", "epoch": 7},
        {"kind": "clear_branches", "initiative": "i", "epoch": 7},
        {"kind": "relaunch", "initiative": "i", "epoch": 7},
    ]


def test_an_initiative_without_a_recorded_exit_stands_by_and_the_lane_goes_to_another():
    """Withheld from fill too: no launch_epic for the gated initiative, while a blocked neighbour still launches."""
    facts = _facts(initiatives=[_initiative("i"), _blocked("b")], run_exited={"i": False})
    assert plan_tick(facts) == [{"kind": "launch_epic", "initiative": "b", "epoch": 7}]


def test_absent_housekeeping_history_emits_one_housekeeping_action():
    assert plan_tick(_facts(), _NOW) == [{"kind": "housekeeping", "reason": "housekeeping due: last never", "epoch": 7}]


def test_housekeeping_one_hour_old_with_a_24_hour_period_is_not_due():
    assert plan_tick(_facts(last_housekeeping_at="2026-09-27T11:00:00+00:00"), _NOW) == []


def test_housekeeping_25_hours_old_is_due():
    last = "2026-09-26T11:00:00+00:00"
    assert plan_tick(_facts(last_housekeeping_at=last), _NOW) == [{"kind": "housekeeping", "reason": f"housekeeping due: last {last}", "epoch": 7}]


def test_a_standby_tick_plans_no_housekeeping():
    assert _kinds(plan_tick(_facts(lease=_lease()), _NOW)) == ["standby"]


def test_a_tick_that_would_emit_two_housekeeping_actions_is_capped_at_one_and_last():
    kinds = _kinds(plan_tick(_facts(approved=[_approved("t1")], initiatives=[_initiative("i")]), _NOW))
    assert (kinds.count("housekeeping"), kinds[-1], "land_phase" in kinds) == (1, "housekeeping", True)


def test_an_initiative_with_unfinished_work_is_homed_on_its_newest_runs_host():
    assert initiative_homes({"demo": "jarvis"}, {"demo"}) == {"demo": "jarvis"}


def test_an_initiative_with_no_unfinished_work_has_no_home_entry():
    assert initiative_homes({"demo": "jarvis"}, set()) == {}


def test_an_empty_newest_run_host_maps_to_the_local_machine():
    assert initiative_homes({"demo": ""}, {"demo"}) == {"demo": ""}
