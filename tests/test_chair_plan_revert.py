from agent_tools.chair_plan import plan_tick
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_types import Facts, LandedMain

_RED: LandedMain = {
    "initiative": "i",
    "phase": "p",
    "repo": "r",
    "pr": 7,
    "commit": "abc",
    "ci": "red",
    "ci_output": "2 failed",
    "smoke": "pending",
    "smoke_output": "",
}
_GREEN: LandedMain = {**_RED, "ci": "green", "ci_output": ""}
_REVERT = {"kind": "revert_land", "initiative": "i", "phase": "p", "repo": "r", "pr": 7, "commit": "abc", "reason": "2 failed"}


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [{"id": "i", "started": True, "ready_tasks": [{"id": "t", "needs": []}], "landed": set()}],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {"i": True},
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


def _main_red(cause: str = "main_red") -> dict:
    return {"task_id": "t", "initiative": "i", "cause": cause, "harness_failures": 0, "has_patch": False, "rescue_failed": False}


def _kinds(actions: list[dict]) -> list[str]:
    return [a["kind"] for a in actions]


def test_one_red_landed_main_plans_one_revert_land():
    reverts = [a for a in plan_tick(_facts(landed_main=[_RED])) if a["kind"] == "revert_land"]
    assert [{k: v for k, v in a.items() if k != "epoch"} for a in reverts] == [_REVERT]


def test_a_green_landed_main_plans_no_revert_land():
    assert "revert_land" not in _kinds(plan_tick(_facts(landed_main=[_GREEN])))


def test_a_revert_land_is_ordered_before_a_launch_in_the_same_plan():
    kinds = _kinds(plan_tick(_facts(landed_main=[_RED])))
    assert kinds.index("revert_land") < kinds.index("relaunch")


def test_a_main_red_quarantine_after_one_revert_recovers_by_relaunch():
    facts = _facts(quarantines=[_main_red()], land_outcomes={"i": ["reverted"]})
    assert plan_recover(facts) == [
        {"kind": "clear_branches", "initiative": "i"},
        {"kind": "relaunch", "initiative": "i"},
    ]


def test_a_main_red_quarantine_after_two_reverts_plans_no_relaunch():
    facts = _facts(quarantines=[_main_red()], land_outcomes={"i": ["reverted", "reverted"]})
    assert plan_recover(facts) == []


def test_a_non_main_red_quarantine_plans_as_before():
    facts = _facts(quarantines=[_main_red("harness") | {"harness_failures": 1}], land_outcomes={"i": ["reverted", "reverted"]})
    assert plan_recover(facts) == [{"kind": "retry", "task_id": "t", "initiative": "i"}]
