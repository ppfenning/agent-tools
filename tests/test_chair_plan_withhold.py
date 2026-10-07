from datetime import UTC, datetime

from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


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


def _initiative(id: str, repo: str, started: bool = False) -> dict:
    return {"id": id, "repo": repo, "started": started, "ready_tasks": [{"id": f"{id}-t", "needs": []}], "landed": set()}


def _launched(actions: list[dict]) -> set[str]:
    return {a["initiative"] for a in actions if a["kind"] in {"launch_epic", "relaunch"}}


def test_an_initiative_in_a_landing_repo_gets_no_launch():
    landing = [{"initiative": "x", "phase": "p", "repo": "r1"}]
    facts = _facts(initiatives=[_initiative("a", "r1")], landing=landing)
    assert _launched(plan_tick(facts, _NOW)) == set()


def test_a_started_initiative_in_a_landing_repo_gets_no_launch():
    landing = [{"initiative": "x", "phase": "p", "repo": "r1"}]
    facts = _facts(initiatives=[_initiative("a", "r1", started=True)], landing=landing)
    assert _launched(plan_tick(facts, _NOW)) == set()


def test_the_same_initiative_launches_when_no_repo_is_landing():
    facts = _facts(initiatives=[_initiative("a", "r1")], landing=[])
    assert _launched(plan_tick(facts, _NOW)) == {"a"}


def test_a_land_phase_planned_this_tick_withholds_its_initiative_and_leaves_another_repo_alone():
    approved = [
        {
            "id": "t1", "initiative": "a", "repo": "r1", "phase": "p", "phase_done": True, "needs": [], "run": "a-1",
            "needs_fetch": False,
        }
    ]
    facts = _facts(approved=approved, initiatives=[_initiative("a", "r9"), _initiative("b", "r2")])
    actions = plan_tick(facts, _NOW)
    assert [a["initiative"] for a in actions if a["kind"] == "land_phase"] == ["a"]
    assert _launched(actions) == {"b"}
