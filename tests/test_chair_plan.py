import copy
from datetime import UTC, datetime, timedelta

import pytest

from agent_tools.chair_facts import initiative_facts
from agent_tools.chair_plan import (
    _free_lanes,
    _launch_cap,
    initiative_homes,
    plan_idle_stall,
    plan_stall,
    plan_tick,
    plan_tick_held,
)
from agent_tools.chair_read_docket import docket_from_rows
from agent_tools.chair_types import Facts
from agent_tools.ci_gate import CiGate

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


def _lease(**fields) -> dict:
    return {"holder": "other", "host": "elsewhere", "epoch": 7, "mine": False, "released": False, "stale": False, **fields}


def _initiative(id: str) -> dict:
    return {"id": id, "started": True, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}


def _blocked(id: str) -> dict:
    """Started with a ready task whose need has not landed: launchable neither as a relaunch nor as a fresh
    epic; it reaches the chair instead, as `waiting on z`."""
    return {"id": id, "started": True, "ready_tasks": [{"id": f"{id}-t", "needs": ["z"]}], "landed": set()}


def _unstarted(id: str) -> dict:
    """Never started, ready task with no needs: launches an epic, never a relaunch."""
    return {"id": id, "started": False, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}


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


def test_a_home_with_a_free_lane_gets_the_relaunch_on_a_tie_and_its_clear_stays_local(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    # friday outweighs jarvis, so _place_on_hosts ranking would pick friday; the tie goes to home, jarvis.
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


def test_a_local_home_leaves_its_clear_without_a_host(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    facts = _facts(run_exited={"a": True}, home={"a": ""})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
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


def _chair_homed_facts(**dispatch_extra) -> Facts:
    return _facts(
        dispatch={**_two_host_dispatch(), "max_in_flight": 2, **dispatch_extra},
        run_exited={"a": True},
        home={"a": ""},
        approved=[_home_approved("a-3")],
    )


def test_a_chair_homed_relaunch_moves_to_a_lane_host_when_local_lanes_are_decompose(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    assert plan_tick(_chair_homed_facts(local_lanes="decompose")) == [
        {"kind": "fetch", "run": "a-3", "repo": "r", "initiative": "a", "epoch": 7},
        {"kind": "clear_branches", "initiative": "a", "carry": ["p"], "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "host": "jarvis", "epoch": 7},
    ]


# The chair is freest here (no live run of its own), so the freest-host rule also keeps it local: a tie goes to the home.
@pytest.mark.parametrize("dispatch_extra", [{"live_runs": 0}, {"live_runs": 0, "local_lanes": "any"}])
def test_a_chair_homed_relaunch_stays_local_when_the_setting_is_absent_or_not_decompose(monkeypatch, dispatch_extra):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    assert plan_tick(_chair_homed_facts(**dispatch_extra)) == [
        {"kind": "clear_branches", "initiative": "a", "carry": ["p"], "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
    ]


def test_a_busy_but_live_home_with_no_run_to_fetch_stays_pinned_and_plans_nothing(monkeypatch):
    _recovering(monkeypatch, [{"kind": "clear_branches", "initiative": "a"}, {"kind": "relaunch", "initiative": "a"}])
    dispatch = {
        "max_in_flight": 1,
        "live_runs": 1,
        "hosts": [{"name": "jarvis", "live_runs": 1, "capacity": 1}, {"name": "friday", "live_runs": 0}],
    }
    facts = _facts(dispatch=dispatch, run_exited={"a": True}, home={"a": "jarvis"}, approved=[_home_approved("")])
    assert plan_tick(facts) == []


def test_a_busy_but_live_home_moves_the_relaunch_to_the_freest_host_with_a_fetch(monkeypatch):
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
    assert plan_tick(facts) == [
        {"kind": "fetch", "run": "a-3", "repo": "r", "initiative": "a", "epoch": 7},
        {"kind": "clear_branches", "initiative": "a", "carry": ["p"], "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "host": "friday", "epoch": 7},
    ]


def _freest_dispatch(local_free: int, **host_free: int) -> dict:
    return {
        "max_in_flight": 5,
        "live_runs": 5 - local_free,
        "hosts": [{"name": name, "live_runs": 0, "capacity": free} for name, free in host_free.items()],
    }


def _relaunch(initiative: str) -> list[dict]:
    return [{"kind": "clear_branches", "initiative": initiative}, {"kind": "relaunch", "initiative": initiative}]


def test_a_local_last_host_with_two_free_lanes_loses_the_relaunch_to_a_lane_host_with_five(monkeypatch):
    _recovering(monkeypatch, _relaunch("a"))
    facts = _facts(dispatch=_freest_dispatch(2, lane=5), run_exited={"a": True}, home={"a": ""})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "host": "lane", "epoch": 7},
    ]


def test_two_relaunches_split_one_each_when_local_and_a_host_each_have_one_free_lane(monkeypatch):
    _recovering(monkeypatch, [*_relaunch("a"), *_relaunch("b")])
    facts = _facts(dispatch=_freest_dispatch(1, lane=1), run_exited={"a": True, "b": True}, home={"a": "", "b": ""})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "clear_branches", "initiative": "b", "epoch": 7},
        {"kind": "relaunch", "initiative": "b", "host": "lane", "epoch": 7},
    ]


def test_a_tie_in_free_lanes_goes_to_the_last_host(monkeypatch):
    _recovering(monkeypatch, _relaunch("a"))
    facts = _facts(dispatch=_freest_dispatch(2, lane=2), run_exited={"a": True}, home={"a": ""})
    assert plan_tick(facts)[-1] == {"kind": "relaunch", "initiative": "a", "epoch": 7}


def test_a_tie_in_free_lanes_goes_to_a_remote_last_host_over_this_machine(monkeypatch):
    _recovering(monkeypatch, _relaunch("a"))
    facts = _facts(dispatch=_freest_dispatch(2, lane=2), run_exited={"a": True}, home={"a": "lane"}, approved=[_home_approved("a-3")])
    assert plan_tick(facts)[-1] == {"kind": "relaunch", "initiative": "a", "host": "lane", "epoch": 7}


def test_a_host_missing_a_required_capability_is_skipped(monkeypatch):
    _recovering(monkeypatch, _relaunch("a"))
    needing = {"id": "a", "started": True, "ready_tasks": [{"id": "a-t", "needs": [], "requires": ["gpu"]}], "landed": set()}
    dispatch = {
        "max_in_flight": 5,
        "live_runs": 3,
        "hosts": [
            {"name": "plain", "live_runs": 0, "capacity": 5},
            {"name": "gpu", "live_runs": 0, "capacity": 3, "capabilities": ["gpu"]},
        ],
    }
    facts = _facts(dispatch=dispatch, initiatives=[needing], run_exited={"a": True}, home={"a": ""})
    assert plan_tick(facts)[-1] == {"kind": "relaunch", "initiative": "a", "host": "gpu", "epoch": 7}


def test_a_rescue_keeps_its_local_slot_while_a_lane_host_has_more_free_lanes(monkeypatch):
    _recovering(monkeypatch, [_RESCUE])
    facts = _facts(dispatch=_freest_dispatch(2, lane=5))
    assert plan_tick(facts) == [{**_RESCUE, "epoch": 7}]


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


def _idle_inputs(**overrides) -> dict:
    return {
        "free_lanes": 1,
        "ready": 1,
        "queued": 0,
        "last_progress_at": "2026-09-27T11:40:00Z",
        "stall_minutes": 15,
        "hosts": [],
        "empty_stubs": [],
        "lands_waiting": [],
        "blocked_ready": [],
        "open_signature": None,
        "open_diagnosis": None,
        **overrides,
    }


def _idle_action(subject: str, reason: str) -> dict:
    return {
        "kind": "needs_chair",
        "initiative": subject,
        "cause": "idle_stall",
        "reason": reason,
        "signature": None,
    }


def test_a_failed_host_plans_one_idle_stall_needs_chair_naming_the_host():
    inputs = _idle_inputs(hosts=[{"host": "box1", "ok": False, "detail": "ssh timed out"}])
    assert plan_idle_stall(inputs, _NOW) == [
        {**_idle_action("box1", "host box1 failed its check: ssh timed out"), "signature": "host:box1"}
    ]


def test_an_empty_stub_plans_one_idle_stall_needs_chair():
    assert plan_idle_stall(_idle_inputs(empty_stubs=["init-a"]), _NOW) == [
        {
            **_idle_action("init-a", "the intake has an initiative stub and no tasks: init-a"),
            "signature": "stub:init-a",
        }
    ]


def test_a_land_waiting_with_checks_not_started_quotes_the_forge_status():
    land = {
        "run": "r1",
        "pr": "42",
        "waiting_since": "2026-09-27T11:00:00Z",
        "checks_started": False,
        "forge_status": "pending",
    }
    assert plan_idle_stall(_idle_inputs(lands_waiting=[land]), _NOW) == [
        {
            **_idle_action("r1", "run r1 pr 42 has not started its checks, forge status: 'pending'"),
            "signature": "land_ci:r1",
        }
    ]


def test_a_task_blocked_by_unlanded_needs_plans_one_idle_stall_needs_chair():
    blocked = [{"task": "t9", "unlanded_needs": ["a", "b"]}]
    assert plan_idle_stall(_idle_inputs(blocked_ready=blocked), _NOW) == [
        {**_idle_action("t9", "task t9 waits on unlanded needs: a, b"), "signature": "blocked_needs:t9"}
    ]


def test_an_unexplained_stall_plans_one_idle_stall_needs_chair_with_the_counts():
    assert plan_idle_stall(_idle_inputs(queued=2), _NOW) == [
        {
            **_idle_action("chair", "unknown free_lanes=1 ready=1 queued=2 lands_waiting=0"),
            "signature": "unknown:chair",
        }
    ]


def test_a_stall_fourteen_minutes_old_plans_nothing():
    assert plan_idle_stall(_idle_inputs(last_progress_at="2026-09-27T11:46:00Z"), _NOW) == []


def test_a_stall_fifteen_minutes_old_plans_one():
    assert _kinds(plan_idle_stall(_idle_inputs(last_progress_at="2026-09-27T11:45:00Z"), _NOW)) == ["needs_chair"]


def test_a_stall_already_recorded_under_the_same_signature_plans_nothing():
    assert plan_idle_stall(_idle_inputs(open_signature="unknown:chair"), _NOW) == []


def test_a_stall_recorded_under_a_different_signature_plans_one():
    actions = plan_idle_stall(_idle_inputs(open_signature="host:box1"), _NOW)
    assert [(a["cause"], a["signature"]) for a in actions] == [("idle_stall", "unknown:chair")]


def test_no_inputs_plan_nothing():
    assert plan_idle_stall(None, _NOW) == []


_QUIET_HOUSEKEEPING = "2026-09-27T11:00:00+00:00"


def test_facts_with_no_idle_stall_key_plan_nothing_from_the_rule():
    assert plan_tick(_facts(last_housekeeping_at=_QUIET_HOUSEKEEPING), _NOW) == []


def test_plan_tick_stamps_the_idle_stall_action_with_the_lease_epoch():
    facts = _facts(idle_stall=_idle_inputs(), last_housekeeping_at=_QUIET_HOUSEKEEPING)
    assert plan_tick(facts, _NOW) == [
        {**_idle_action("chair", "unknown free_lanes=1 ready=1 queued=0 lands_waiting=0"), "signature": "unknown:chair", "epoch": 7}
    ]


def test_the_idle_stall_action_passes_the_hard_stop_and_a_zero_launch_cap():
    limits = {"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 0, "go_degraded": False}
    facts = _facts(limits=limits, idle_stall=_idle_inputs(), last_housekeeping_at=_QUIET_HOUSEKEEPING)
    assert [a["cause"] for a in plan_tick(facts, _NOW)] == ["idle_stall"]


def test_the_idle_stall_action_is_not_counted_against_the_launch_cap():
    limits = {"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False}
    facts = _facts(
        limits=limits,
        initiatives=[_initiative("a"), _initiative("b"), _initiative("c")],
        run_exited={"a": True, "b": True, "c": True},
        idle_stall=_idle_inputs(),
        last_housekeeping_at=_QUIET_HOUSEKEEPING,
    )
    actions = plan_tick(facts, _NOW)
    assert [(a["kind"], a["initiative"]) for a in actions] == [
        ("needs_chair", "chair"),
        ("clear_branches", "a"),
        ("relaunch", "a"),
        ("clear_branches", "b"),
        ("relaunch", "b"),
    ]


def test_free_lanes_is_the_launch_cap_minus_kept_launches_when_the_cap_binds():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 3, "go_degraded": False},
        dispatch={"max_in_flight": 9, "live_runs": 0, "hosts": []},
        initiatives=[_initiative("a"), _unstarted("b"), _unstarted("c"), _unstarted("d")],
        run_exited={"a": True},
    )
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
        {"kind": "launch_epic", "initiative": "b", "epoch": 7},
        {"kind": "launch_epic", "initiative": "c", "epoch": 7},
    ]


def test_a_relaunched_initiative_is_not_also_launched_as_an_epic():
    facts = _facts(initiatives=[_initiative("a"), _initiative("b"), _unstarted("c")], run_exited={"a": True, "b": True})
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
        initiatives=[_unstarted("a"), _unstarted("b"), _unstarted("c")],
    )
    assert _kinds(plan_tick(facts)) == ["launch_epic", "launch_epic"]


def test_dispatch_lanes_are_counted_after_the_kept_launches():
    facts = _facts(
        limits={"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        dispatch={"max_in_flight": 3, "live_runs": 0, "hosts": []},
        initiatives=[_initiative("a"), _unstarted("b"), _unstarted("c"), _unstarted("d")],
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
    facts = _facts(initiatives=[_blocked("m"), _unstarted("b")], quarantines=[_SCOPE_QUARANTINE])
    assert plan_tick(facts) == [
        {"kind": "needs_chair", "initiative": "m", "cause": "scope", "epoch": 7},
        {"kind": "launch_epic", "initiative": "b", "epoch": 7},
    ]


def test_a_retried_initiative_is_not_also_launched_as_an_epic():
    retry = {"task_id": "q1", "initiative": "a", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False}
    facts = _facts(initiatives=[_blocked("a"), _unstarted("b")], quarantines=[retry])
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


_SCHEMA_REASON = "the lane host's graphs is older than the shared store; runs i-2 and i-1 both died on a schema-version refusal"


def test_two_schema_version_deaths_plan_needs_chair_and_no_relaunch():
    """The unstarted neighbour still launches, so only the schema-dead initiative is withheld from fill."""
    facts = _facts(
        initiatives=[_initiative("i"), _unstarted("b")],
        run_exited={"i": True},
        schema_deaths={"i": ["i-2", "i-1"]},
    )
    assert plan_tick(facts) == [
        {"kind": "needs_chair", "initiative": "i", "cause": "schema_version", "reason": _SCHEMA_REASON, "epoch": 7},
        {"kind": "launch_epic", "initiative": "b", "epoch": 7},
    ]


def test_a_schema_death_initiative_with_a_harness_quarantine_plans_no_retry():
    retry = {"task_id": "q", "initiative": "i", "cause": "harness", "harness_failures": 1, "has_patch": False, "rescue_failed": False}
    facts = _facts(initiatives=[_initiative("i")], quarantines=[retry], schema_deaths={"i": ["i-2", "i-1"]})
    assert plan_tick(facts) == [
        {"kind": "needs_chair", "initiative": "i", "cause": "schema_version", "reason": _SCHEMA_REASON, "epoch": 7},
    ]


def test_an_initiative_absent_from_schema_deaths_still_relaunches():
    facts = _facts(initiatives=[_initiative("i")], run_exited={"i": True}, schema_deaths={})
    assert plan_tick(facts) == [
        {"kind": "clear_branches", "initiative": "i", "epoch": 7},
        {"kind": "relaunch", "initiative": "i", "epoch": 7},
    ]


def test_a_schema_death_initiative_that_is_also_lost_plans_needs_chair_and_no_relaunch():
    facts = _facts(
        initiatives=[_initiative("i")],
        lost_runs={"i": "i-2"},
        schema_deaths={"i": ["i-2", "i-1"]},
    )
    assert plan_tick(facts) == [
        {"kind": "needs_chair", "initiative": "i", "cause": "schema_version", "reason": _SCHEMA_REASON, "epoch": 7},
        {"kind": "mark_lost", "initiative": "i", "run": "i-2", "epoch": 7},
    ]


def test_an_initiative_without_a_recorded_exit_stands_by_and_the_lane_goes_to_another():
    """Withheld from fill too: no launch_epic for the gated initiative, while an unstarted neighbour still launches."""
    facts = _facts(initiatives=[_initiative("i"), _unstarted("b")], run_exited={"i": False})
    assert plan_tick(facts) == [{"kind": "launch_epic", "initiative": "b", "epoch": 7}]


def test_a_ready_task_whose_single_need_is_blocked_is_not_launchable():
    """The ticket's own case: an unstarted initiative's only ready task needs `z`, which has not landed.
    Not launchable this cycle, and reported exactly once as `waiting on z`."""
    facts = _facts(initiatives=[{**_blocked("b"), "started": False}])
    assert plan_tick(facts) == [{"kind": "needs_chair", "initiative": "b", "cause": "waiting on z", "epoch": 7}]


def test_the_same_fixture_with_the_need_done_is_launchable():
    facts = _facts(initiatives=[{**_blocked("b"), "started": False, "landed": {"z"}}])
    assert plan_tick(facts) == [{"kind": "launch_epic", "initiative": "b", "epoch": 7}]


_INCIDENT = "allocate-short-initiative-ids-from-a-store"


def _store_row(task_id: str, phase: str, state: str, needs: tuple[str, ...] = ()) -> dict:
    return {
        "kind": "task", "initiative": _INCIDENT, "task_id": task_id, "phase": phase, "state": state,
        "needs": list(needs), "title": task_id, "surfaces": [], "body": "", "extra": {},
        "holder": None, "epoch": 0, "expires_at": "",
    }


def _incident_facts(migration_state: str) -> Facts:
    """The 2026-09-29 board as store rows, through the production reader, not hand-built InitiativeFacts."""
    rows = [
        _store_row("write-id-design-note", "1-schema", "done"),
        _store_row("add-id-sequence-schema-migration", "1-schema", migration_state),
        _store_row("implement-store-ids-module", "2-module", "ready", ("add-id-sequence-schema-migration",)),
    ]
    docket = {"initiatives": docket_from_rows(rows, "2026-09-29T23:00:00Z")}
    return _facts(initiatives=initiative_facts(docket, set()), run_exited={_INCIDENT: True})


def test_the_incident_board_read_from_store_rows_is_not_relaunched_and_waits_on_the_migration():
    assert plan_tick(_incident_facts("blocked")) == [
        {"kind": "needs_chair", "initiative": _INCIDENT, "cause": "waiting on add-id-sequence-schema-migration", "epoch": 7}
    ]


def test_the_incident_board_read_from_store_rows_relaunches_once_the_migration_is_done():
    assert plan_tick(_incident_facts("done")) == [
        {"kind": "clear_branches", "initiative": _INCIDENT, "epoch": 7},
        {"kind": "relaunch", "initiative": _INCIDENT, "epoch": 7},
    ]


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


def test_a_run_on_the_chairs_own_host_homes_its_initiative_on_this_machine():
    """Local runs record the chair's hostname (2026-10-05: bp-macbook), not a blank; homed by name, placement
    looked for a lane host called bp-macbook and dropped every relaunch homed on the chair's machine."""
    assert initiative_homes({"demo": "bp-macbook", "other": "jarvis"}, {"demo", "other"}, local_host="bp-macbook") == {
        "demo": "", "other": "jarvis"}


def _remote(**overrides) -> Facts:
    """Initiative a with one live remote run a-3, fetched and not yet exited."""
    live = _stall_candidate(run="a-3", initiative="a", local=False, last_call=None)
    return _facts(**{"initiatives": [_initiative("a")], "stall_candidates": [live], **overrides})


def _probe(alive: bool, minutes: int) -> dict:
    return {"a": {"alive": alive, "last_beat_at": (_NOW - timedelta(minutes=minutes)).isoformat()}}


def test_a_dead_pid_silent_eleven_minutes_plans_exactly_what_a_lost_runs_entry_plans():
    actions = plan_tick(_remote(pid_probe=_probe(False, 11)), _NOW)
    assert (actions, {"kind": "mark_lost", "initiative": "a", "run": "a-3", "epoch": 7} in actions) == (
        plan_tick(_remote(lost_runs={"a": "a-3"}), _NOW),
        True,
    )


def test_a_dead_pid_silent_nine_minutes_plans_as_if_unprobed():
    assert plan_tick(_remote(pid_probe=_probe(False, 9)), _NOW) == plan_tick(_remote(), _NOW)


def test_a_live_pid_silent_for_hours_plans_as_if_unprobed():
    assert plan_tick(_remote(pid_probe=_probe(True, 600)), _NOW) == plan_tick(_remote(), _NOW)


def test_a_dead_pid_already_in_lost_runs_is_planned_once():
    lost = {"a": "a-3"}
    assert plan_tick(_remote(pid_probe=_probe(False, 11), lost_runs=lost), _NOW) == plan_tick(_remote(lost_runs=lost), _NOW)


def test_a_dead_pid_whose_run_has_exited_is_not_lost():
    exited = {"a": True}
    assert plan_tick(_remote(pid_probe=_probe(False, 11), run_exited=exited), _NOW) == plan_tick(_remote(run_exited=exited), _NOW)


def test_a_dead_pid_whose_exit_is_not_yet_fetched_is_not_lost_this_tick():
    unfetched = {"a": "a-3"}
    actions = plan_tick(_remote(pid_probe=_probe(False, 11), remote_unfetched=unfetched), _NOW)
    assert (actions, "fetch_exit" in _kinds(actions), "mark_lost" in _kinds(actions)) == (
        plan_tick(_remote(remote_unfetched=unfetched), _NOW),
        True,
        False,
    )


def test_a_dead_pid_with_no_live_remote_run_is_not_lost():
    local = _facts(initiatives=[_initiative("a")], stall_candidates=[_stall_candidate(run="a-3", initiative="a", last_call=None)])
    assert plan_tick({**local, "pid_probe": _probe(False, 11)}, _NOW) == plan_tick(local, _NOW)  # type: ignore[arg-type]


def test_a_dead_pid_for_an_initiative_absent_from_the_facts_is_not_lost():
    stray = {"b": _probe(False, 11)["a"]}
    live_b = _stall_candidate(run="b-1", initiative="b", local=False, last_call=None)
    base = _remote(stall_candidates=[live_b])
    assert plan_tick({**base, "pid_probe": stray}, _NOW) == plan_tick(base, _NOW)  # type: ignore[arg-type]


def test_a_malformed_probe_entry_or_probe_fact_plans_as_if_unprobed():
    entries = [{"alive": False}, {"alive": False, "last_beat_at": "yesterday"}, {"alive": False, "last_beat_at": 5}, "dead"]
    probes = [*({"a": e} for e in entries), None, "dead", ["a"]]
    assert [plan_tick(_remote(pid_probe=p), _NOW) for p in probes] == [plan_tick(_remote(), _NOW)] * len(probes)


def test_a_naive_last_beat_is_not_lost():
    naive = {"a": {"alive": False, "last_beat_at": "2026-09-27T11:00:00"}}
    assert plan_tick(_remote(pid_probe=naive), _NOW) == plan_tick(_remote(), _NOW)


def test_a_dead_pid_at_the_hard_stop_reaches_the_chair_as_a_lost_run_does():
    stop = {"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False}
    assert plan_tick(_remote(pid_probe=_probe(False, 11), limits=stop), _NOW) == plan_tick(
        _remote(lost_runs={"a": "a-3"}, limits=stop), _NOW
    )


def test_a_dead_pid_is_not_lost_when_the_tick_has_no_clock():
    assert plan_tick(_remote(pid_probe=_probe(False, 11))) == plan_tick(_remote())


def _fetching(id: str, run: str) -> dict:
    return {**_approved(id), "id": f"{id}-t", "initiative": id, "run": run, "needs_fetch": True}


def _blocked_host_facts(b_ok: bool | None = False) -> Facts:
    checked = _NOW.isoformat()
    return _facts(
        approved=[_fetching("x", "x-1"), _fetching("y", "y-1")],
        initiatives=[_initiative("i"), _initiative("j")],
        run_exited={"i": True, "j": True},
        run_hosts={"x-1": "b", "y-1": "c"},
        initiative_homes={"i": "b", "j": "c"},
        login_hosts=[_login_host("b", login_ok=b_ok, checked_at=checked), _login_host("c", login_ok=True, checked_at=checked)],
    )


def _subjects(actions: list[dict], kind: str) -> list[str]:
    return [a.get("run") or a["initiative"] for a in actions if a["kind"] == kind]


def test_a_blocked_hosts_fetch_is_dropped_while_another_hosts_fetch_is_kept():
    assert _subjects(plan_tick(_blocked_host_facts()), "fetch") == ["y-1"]


def test_a_blocked_hosts_clear_branches_is_dropped_while_another_hosts_clear_is_kept():
    assert _subjects(plan_tick(_blocked_host_facts()), "clear_branches") == ["j"]


def test_a_blocked_hosts_land_is_dropped_while_another_hosts_land_is_kept():
    assert _subjects(plan_tick(_blocked_host_facts()), "land_phase") == ["y-1"]


def test_a_blocked_hosts_needs_chair_line_appears_exactly_once():
    needs = [a for a in plan_tick(_blocked_host_facts()) if a["kind"] == "needs_chair"]
    assert needs == [{"kind": "needs_chair", "host": "b", "cause": "login_lapsed", "epoch": 7}]


def test_a_host_whose_login_is_ok_plans_its_fetch_clear_and_land_again():
    actions = plan_tick(_blocked_host_facts(b_ok=True))
    assert _subjects(actions, "fetch") == ["x-1", "y-1"]
    assert _subjects(actions, "clear_branches") == ["i", "j"]
    assert _subjects(actions, "land_phase") == ["x-1", "y-1"]


def test_facts_without_host_keys_plan_as_before():
    facts = _facts(approved=[_fetching("x", "x-1")], initiatives=[_initiative("i")], run_exited={"i": True})
    assert plan_tick(facts) == [
        {"kind": "fetch", "run": "x-1", "repo": "r", "initiative": "x", "epoch": 7},
        {"kind": "land_phase", "initiative": "x", "phase": "p", "repo": "r", "run": "x-1", "epoch": 7},
        {"kind": "clear_branches", "initiative": "i", "epoch": 7},
        {"kind": "relaunch", "initiative": "i", "epoch": 7},
    ]


_PAUSED = CiGate(True, "forge incident")
_WAITING_ON_LAND = [{"kind": "needs_chair", "initiative": "l", "cause": "waiting on l-a", "epoch": 7}]


def _land_only(id: str) -> dict:
    """Started, nothing ready: its one waiting task needs `<id>-a`, which is approved and not yet landed."""
    return {
        "id": id, "started": True, "ready_tasks": [],
        "waiting_tasks": [{"id": f"{id}-w", "needs": [f"{id}-a"], "requires": []}], "landed": set(),
    }


def _pending_land(id: str) -> dict:
    return {**_approved(f"{id}-a"), "initiative": id, "phase_done": False}


def test_paused_with_a_land_only_initiative_lists_it_in_held_for_ci_and_plans_no_relaunch():
    facts = _facts(initiatives=[_land_only("l")], approved=[_pending_land("l")], run_exited={"l": True})
    plan = plan_tick_held(facts, None, _PAUSED)
    assert plan.held_for_ci == ("l",)
    assert plan.actions == _WAITING_ON_LAND


def test_paused_drops_the_relaunch_pair_a_lost_run_would_plan_for_a_land_only_initiative():
    facts = _facts(initiatives=[_land_only("l")], approved=[_pending_land("l")], lost_runs={"l": "r1"})
    assert _kinds(plan_tick_held(facts).actions) == ["needs_chair", "mark_lost", "clear_branches", "relaunch"]
    plan = plan_tick_held(facts, None, _PAUSED)
    assert _kinds(plan.actions) == ["needs_chair", "mark_lost"]
    assert plan.held_for_ci == ("l",)


def test_paused_with_an_independent_initiative_still_relaunches_it():
    facts = _facts(initiatives=[_initiative("i")], run_exited={"i": True})
    plan = plan_tick_held(facts, None, _PAUSED)
    assert plan.held_for_ci == ()
    assert plan.actions == [
        {"kind": "clear_branches", "initiative": "i", "epoch": 7},
        {"kind": "relaunch", "initiative": "i", "epoch": 7},
    ]


def test_paused_with_a_land_gated_waiting_task_and_an_independent_ready_task_relaunches_for_the_independent_work():
    mixed = {**_land_only("m"), "ready_tasks": [{"id": "m-t", "needs": [], "requires": []}]}
    facts = _facts(initiatives=[mixed], approved=[_pending_land("m")], run_exited={"m": True})
    plan = plan_tick_held(facts, None, _PAUSED)
    assert plan.held_for_ci == ()
    assert _kinds(plan.actions) == ["clear_branches", "relaunch"]


def test_paused_with_a_waiting_task_blocked_by_something_other_than_a_land_is_not_held():
    facts = _facts(initiatives=[_land_only("l")], approved=[], lost_runs={"l": "r1"})
    plan = plan_tick_held(facts, None, _PAUSED)
    assert plan.held_for_ci == ()
    assert _kinds(plan.actions) == ["needs_chair", "mark_lost", "clear_branches", "relaunch"]


def test_paused_with_a_fresh_initiative_and_no_pending_land_still_launches_it():
    plan = plan_tick_held(_facts(initiatives=[_unstarted("f")]), None, _PAUSED)
    assert plan.held_for_ci == ()
    assert plan.actions == [{"kind": "launch_epic", "initiative": "f", "epoch": 7}]


def test_unpaused_matches_plan_tick_and_holds_nothing():
    land_only = _facts(initiatives=[_land_only("l")], approved=[_pending_land("l")], lost_runs={"l": "r1"})
    mixed = _facts(initiatives=[_initiative("i"), _unstarted("f")], run_exited={"i": True})
    for facts in (land_only, mixed):
        plan = plan_tick_held(facts, _NOW, CiGate(False, ""))
        assert plan.actions == plan_tick(facts, _NOW)
        assert plan.held_for_ci == ()


def test_a_tick_the_lease_gates_holds_nothing_even_when_paused():
    facts = _facts(lease=_lease(), initiatives=[_land_only("l")], approved=[_pending_land("l")])
    assert plan_tick_held(facts, None, _PAUSED).held_for_ci == ()
