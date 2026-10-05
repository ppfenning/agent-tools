from agent_tools.chair_plan_recover import claimed_by, plan_recover
from agent_tools.chair_types import Facts, InitiativeFacts, QuarantineFacts, RunningInitiative


def _initiative(id: str, surfaces: list[str], repo: str = "r") -> InitiativeFacts:
    return {
        "id": id,
        "started": True,
        "ready_tasks": [{"id": "t1", "needs": []}],  # type: ignore[typeddict-item]
        "landed": set(),
        "repo": repo,
        "ready_surfaces": surfaces,
    }


def _quarantine(initiative: str, has_patch: bool = False) -> QuarantineFacts:
    return {
        "task_id": "q1",
        "initiative": initiative,
        "cause": "harness",
        "harness_failures": 1,
        "has_patch": has_patch,
        "rescue_failed": False,
    }


def _running(id: str, surfaces: list[str]) -> RunningInitiative:
    return {"id": id, "repo": "r", "surfaces": surfaces}


def _facts(
    initiatives: list[InitiativeFacts],
    quarantines: list[QuarantineFacts] | None = None,
    running: list[RunningInitiative] | None = None,
    streaks: dict[str, int] | None = None,
) -> Facts:
    return {
        "lease": {"holder": "a", "host": "h", "epoch": 1, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        "dispatch": {"max_in_flight": 3, "live_runs": 0},
        "approved": [],
        "initiatives": initiatives,
        "quarantines": [] if quarantines is None else quarantines,
        "intake": [],
        "work_store_ready": True,
        "sources_configured": True,
        "running": [] if running is None else running,
        "steer_streaks": {} if streaks is None else streaks,
    }


CLEAR = {"kind": "clear_branches", "initiative": "x"}
STEER = {"kind": "steer_clear", "initiative": "x", "other": "y", "paths": ["a.py"]}


def test_a_relaunch_overlapping_a_running_initiative_is_replaced_by_a_steer_clear():
    facts = _facts([_initiative("x", ["a.py"])], running=[_running("y", ["a.py"])])
    assert plan_recover(facts) == [STEER]


def test_a_retry_overlapping_a_running_initiative_is_replaced_by_a_steer_clear():
    facts = _facts([_initiative("x", ["a.py"])], [_quarantine("x")], [_running("y", ["a.py"])])
    assert plan_recover(facts) == [STEER]


def test_a_rescue_overlapping_a_running_initiative_is_replaced_by_a_steer_clear():
    facts = _facts([_initiative("x", ["a.py"])], [_quarantine("x", has_patch=True)], [_running("y", ["a.py"])])
    assert plan_recover(facts) == [STEER]


def test_a_relaunch_disjoint_from_a_running_initiative_is_unchanged():
    facts = _facts([_initiative("x", ["a.py"])], running=[_running("y", ["b.py"])])
    assert plan_recover(facts) == [CLEAR, {"kind": "relaunch", "initiative": "x"}]


def test_the_recovered_initiative_does_not_steer_clear_of_its_own_stale_lane():
    facts = _facts([_initiative("x", ["a.py"])], running=[_running("x", ["a.py"])])
    assert plan_recover(facts) == [CLEAR, {"kind": "relaunch", "initiative": "x"}]


def test_two_overlapping_relaunches_keep_the_first_and_defer_the_second():
    facts = _facts([_initiative("x", ["a.py"]), _initiative("y", ["a.py"])])
    assert plan_recover(facts) == [
        CLEAR,
        {"kind": "relaunch", "initiative": "x"},
        {"kind": "steer_clear", "initiative": "y", "other": "x", "paths": ["a.py"]},
    ]


def test_a_pair_deferred_three_times_needs_the_chair():
    facts = _facts([_initiative("x", ["a.py"])], running=[_running("y", ["a.py"])], streaks={"x|y": 3})
    assert plan_recover(facts) == [
        {
            "kind": "needs_chair",
            "initiative": "x",
            "cause": "steer_deferred",
            "reason": "x shares a.py with running y; the pair has been deferred three ticks",
        }
    ]


def test_claimed_by_returns_the_entry_of_a_kept_relaunch():
    facts = _facts([_initiative("x", ["a.py", "b/"])])
    assert claimed_by(plan_recover(facts), facts) == [{"id": "x", "repo": "r", "surfaces": ["a.py", "b/"]}]


def test_claimed_by_omits_a_deferred_launch():
    facts = _facts([_initiative("x", ["a.py"])], running=[_running("y", ["a.py"])])
    assert claimed_by(plan_recover(facts), facts) == []


def test_claimed_by_returns_a_kept_retry_and_a_kept_rescue():
    facts = _facts([_initiative("x", ["a.py"]), _initiative("z", ["c.py"])], [_quarantine("x"), _quarantine("z", True)])
    assert [e["id"] for e in claimed_by(plan_recover(facts), facts)] == ["x", "z"]


def test_claimed_by_of_two_overlapping_relaunches_returns_only_the_kept_one():
    facts = _facts([_initiative("x", ["a.py"]), _initiative("y", ["a.py"])])
    assert claimed_by(plan_recover(facts), facts) == [{"id": "x", "repo": "r", "surfaces": ["a.py"]}]


def test_claimed_by_returns_one_entry_for_two_retries_of_one_initiative():
    second = _quarantine("x")
    second["task_id"] = "q2"
    facts = _facts([_initiative("x", ["a.py"])], [_quarantine("x"), second])
    assert claimed_by(plan_recover(facts), facts) == [{"id": "x", "repo": "r", "surfaces": ["a.py"]}]
