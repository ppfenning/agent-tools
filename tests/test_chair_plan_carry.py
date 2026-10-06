from agent_tools.chair_plan import plan_tick
from tests.test_chair_plan import _approved, _facts, _initiative, _kinds

_ROW = {"task": "t1", "run": "x-1", "host": "h", "branch": "agents/x-1", "commit": "abc", "needs": [], "run_seq": 1}
_STRANDED = {"initiative": "x", "phase": "p", "phase_branch": "pr/x-p", "approved": [_ROW], "pending": []}
_BRANCH = {"initiative": "a", "phase": "p", "branch": "pr/a-p", "ahead": 2, "behind": 3, "tip": "0123456789abcdef"}
_RELAUNCHING = {"initiatives": [_initiative("a")], "run_exited": {"a": True}}


def _limits(**fields) -> dict:
    return {"hard_stop": False, "weekly_fraction": 0.5, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False, **fields}


def test_a_stranded_phase_gives_one_carry_phase_and_no_land_for_that_phase():
    plan = plan_tick(_facts(approved=[_approved("t1")], stranded=[_STRANDED]))
    assert _kinds(plan) == ["carry_phase"]
    assert plan[0]["pr_branch"] == "pr/carry-x-p"
    assert plan[0]["epoch"] == 7


def test_a_land_for_another_phase_is_kept_beside_the_carry():
    other = {**_approved("t2"), "phase": "q"}
    plan = plan_tick(_facts(approved=[_approved("t1"), other], stranded=[_STRANDED]))
    assert [(a["kind"], a["phase"]) for a in plan] == [("land_phase", "q"), ("carry_phase", "p")]


def test_a_behind_and_ahead_branch_of_a_relaunched_initiative_is_rebased_directly_before_the_relaunch():
    plan = plan_tick(_facts(**_RELAUNCHING, phase_branches=[_BRANCH]))
    assert _kinds(plan) == ["clear_branches", "rebase_phase", "relaunch"]
    assert plan[1] == {
        "kind": "rebase_phase", "initiative": "a", "phase": "p", "branch": "pr/a-p", "tip": "0123456789abcdef", "base": "main", "epoch": 7,
    }


def test_a_rebase_goes_before_the_launch_epic_of_its_initiative():
    fresh = {**_initiative("a"), "started": False}
    plan = plan_tick(_facts(initiatives=[fresh], phase_branches=[_BRANCH]))
    assert _kinds(plan) == ["rebase_phase", "launch_epic"]


def test_a_branch_of_an_initiative_not_relaunched_gives_no_rebase():
    plan = plan_tick(_facts(initiatives=[_initiative("a")], phase_branches=[_BRANCH]))
    assert "rebase_phase" not in _kinds(plan)


def test_a_zero_launch_cap_still_plans_carry_phase_and_rebase_phase():
    facts = _facts(**_RELAUNCHING, limits=_limits(launch_cap=0), phase_branches=[_BRANCH], stranded=[_STRANDED])
    assert _kinds(plan_tick(facts)) == ["carry_phase", "rebase_phase"]


def test_a_hard_stop_keeps_carry_phase_and_launches_nothing():
    facts = _facts(**_RELAUNCHING, limits=_limits(hard_stop=True), phase_branches=[_BRANCH], stranded=[_STRANDED])
    assert _kinds(plan_tick(facts)) == ["carry_phase"]


def test_facts_without_stranded_or_phase_branches_plan_as_before():
    plan = plan_tick(_facts(**_RELAUNCHING))
    assert plan == [
        {"kind": "clear_branches", "initiative": "a", "epoch": 7},
        {"kind": "relaunch", "initiative": "a", "epoch": 7},
    ]
