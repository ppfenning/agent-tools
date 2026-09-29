from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts


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


def test_an_empty_decompose_entry_becomes_a_needs_chair_item():
    facts = _facts(empty_decompose=[{"initiative": "m", "run": "m-1"}])
    assert plan_tick(facts) == [
        {"kind": "needs_chair", "initiative": "m", "run": "m-1", "cause": "empty_decompose", "epoch": 7},
    ]


def test_an_empty_decompose_needs_chair_item_passes_through_at_the_hard_stop():
    facts = _facts(
        limits={"hard_stop": True, "weekly_fraction": 0.95, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        empty_decompose=[{"initiative": "m", "run": "m-1"}],
    )
    assert plan_tick(facts) == [
        {"kind": "needs_chair", "initiative": "m", "run": "m-1", "cause": "empty_decompose", "epoch": 7},
    ]
