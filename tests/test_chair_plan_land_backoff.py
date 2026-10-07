from datetime import UTC, datetime, timedelta

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_land import backoff_held, plan_lands

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _facts(run: str = "i-1", **extra) -> dict:
    task = {
        "id": "a", "initiative": "i", "repo": "r", "phase": "p", "phase_done": True, "needs": [], "run": run,
        "needs_fetch": True,
    }
    return {"approved": [task], "initiatives": [{"id": "i", "started": True, "ready_tasks": [], "landed": set()}], **extra}


def _refusal(run: str = "i-1", attempts: int = 1) -> dict:
    return {"initiative": "i", "phase": "p", "run": run, "attempts": attempts, "last_refused_at": T0.isoformat()}


def _kinds(facts: dict, now: datetime | None) -> list[str]:
    return [a["kind"] for a in plan_lands(facts, now)]


def test_a_refusal_for_the_same_run_inside_its_window_plans_no_fetch_and_no_land():
    assert _kinds(_facts(land_refusals=[_refusal()]), T0 + timedelta(minutes=5)) == []


def test_a_new_run_id_plans_the_fetch_and_the_land():
    facts = _facts(run="i-2", land_refusals=[_refusal(run="i-1")])
    assert _kinds(facts, T0 + timedelta(minutes=5)) == ["fetch", "land_phase"]


def test_an_expired_window_plans_the_fetch_and_the_land():
    assert _kinds(_facts(land_refusals=[_refusal()]), T0 + timedelta(minutes=15)) == ["fetch", "land_phase"]


def test_without_now_a_refusal_changes_nothing():
    assert _kinds(_facts(land_refusals=[_refusal()]), None) == ["fetch", "land_phase"]


def test_an_absent_facts_key_plans_as_before():
    assert _kinds(_facts(), T0) == ["fetch", "land_phase"]


def test_a_refusal_whose_phase_has_landed_holds_nothing():
    facts = {**_facts(land_refusals=[_refusal()]), "approved": []}
    assert (plan_lands(facts, T0), backoff_held(facts, T0)) == ([], frozenset())


def test_backoff_held_names_only_a_suppressed_group():
    assert backoff_held(_facts(land_refusals=[_refusal()]), T0) == frozenset({"i"})
    assert backoff_held(_facts(land_refusals=[_refusal()]), T0 + timedelta(minutes=15)) == frozenset()
    assert backoff_held(_facts(land_refusals=[_refusal()]), None) == frozenset()


def test_a_naive_now_against_an_aware_stamp_and_a_partial_row_do_not_raise():
    naive = (T0 + timedelta(minutes=5)).replace(tzinfo=None)
    assert _kinds(_facts(land_refusals=[_refusal()]), naive) == []
    assert _kinds(_facts(land_refusals=[{"initiative": "i", "phase": "p", "run": "i-1"}]), T0) == ["fetch", "land_phase"]


def _tick_facts(**extra) -> dict:
    ready = {"id": "i", "started": True, "ready_tasks": [{"id": "other", "needs": []}], "landed": set(), "repo": "r"}
    return {
        **_facts(**extra),
        "initiatives": [ready],
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {"i": True},
        "schema_deaths": {},
        "lost_runs": {},
        "last_housekeeping_at": T0.isoformat(),
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }


def _tick_kinds(facts: dict, now: datetime) -> list[tuple[str, str]]:
    return [(a["kind"], a.get("initiative", "")) for a in plan_tick(facts, now) if a["kind"] != "housekeeping"]


def test_a_tick_inside_the_window_plans_no_land_and_still_holds_the_initiative_from_launching():
    # Unstarted, so recover plans no relaunch and only the land hold stands between fill and a launch_epic.
    facts = _tick_facts(land_refusals=[_refusal()])
    unstarted = {**facts, "initiatives": [{**facts["initiatives"][0], "started": False}]}
    assert ("launch_epic", "i") in _tick_kinds({**unstarted, "approved": []}, T0)
    kinds = _tick_kinds(unstarted, T0 + timedelta(minutes=5))
    assert not [k for k in kinds if k[1] == "i" and k[0] in {"fetch", "land_phase", "launch_epic"}]


def test_a_tick_passes_now_to_fill_so_the_launch_budget_refuses_a_launch_epic():
    history = {"i": {"launches": [{"at": T0.isoformat(), "kind": "launch_epic"}]}}
    facts = {**_tick_facts(launch_history=history, max_launches_per_hour=1), "approved": [], "run_exited": {}}
    unstarted = [{**facts["initiatives"][0], "started": False}]
    assert ("launch_epic", "i") in _tick_kinds({**facts, "initiatives": unstarted, "launch_history": {}}, T0)
    assert ("launch_epic", "i") not in _tick_kinds({**facts, "initiatives": unstarted}, T0)


def test_a_tick_after_the_window_plans_the_fetch_and_the_land():
    kinds = _tick_kinds(_tick_facts(land_refusals=[_refusal()]), T0 + timedelta(minutes=15))
    assert ("fetch", "i") in kinds and ("land_phase", "i") in kinds
