from agent_tools.chair_exec import Deps, perform
from agent_tools.chair_plan_recover import plan_recover
from agent_tools.chair_types import Action, Facts, QuarantineFacts

LAND = ["cox", "runs", "land", "r-9", "--repo", "/repo", "--task", "q1", "--apply"]


def _fact(causes: list[str], reason: str = "", cause: str | None = None) -> QuarantineFacts:
    return {
        "task_id": "q1",
        "initiative": "i",
        "cause": causes[-1] if cause is None else cause,
        "harness_failures": 0,
        "has_patch": False,
        "rescue_failed": False,
        "reason": reason,
        "causes": causes,
        "run": "r-9",
        "repo": "/repo",
    }


def _plan(q: QuarantineFacts) -> Action:
    facts: Facts = {"quarantines": [q], "approved": [], "initiatives": []}  # type: ignore[typeddict-item]
    (action,) = plan_recover(facts)
    return action


def test_a_long_reason_is_cut_to_600_and_the_first_cause_is_kept() -> None:
    action = _plan(_fact(["ticket", "stranded"], reason="x" * 900))
    assert len(action["reason"]) == 600
    assert action["cause"] == "ticket"


def test_a_stranded_cause_plans_carry_with_the_land_argv() -> None:
    action = _plan(_fact(["stranded"]))
    assert (action["remedy"], action["command"]) == ("carry", LAND)


def test_a_ticket_cause_plans_re_ground() -> None:
    assert _plan(_fact(["ticket"]))["remedy"] == "re-ground"


def test_a_fact_with_no_causes_list_uses_its_own_cause() -> None:
    q = _fact([], cause="stranded")
    assert _plan(q)["cause"] == "stranded"


def test_a_fact_without_a_row_reason_plans_the_old_needs_chair() -> None:
    q = {k: v for k, v in _fact(["ticket"]).items() if k not in ("reason", "causes", "run", "repo")}
    assert _plan(q) == {"kind": "needs_chair", "initiative": "i", "task_id": "q1", "cause": "ticket"}  # type: ignore[arg-type]


def test_a_waiting_needs_chair_is_unchanged() -> None:
    facts: Facts = {
        "quarantines": [],
        "approved": [],
        "initiatives": [{"id": "i", "started": True, "ready_tasks": [{"id": "t1", "needs": ["a"]}], "landed": set()}],
    }  # type: ignore[typeddict-item]
    assert plan_recover(facts) == [{"kind": "needs_chair", "initiative": "i", "cause": "waiting on a"}]


def test_the_recorded_line_holds_all_four_fields_whole() -> None:
    recorded: list[Action] = []
    deps = Deps(
        run=lambda argv: (0, ""),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=recorded.append,
        run_id=lambda action: "r-9",
        repo_for=lambda action: "/repo",
    )
    action = {**_plan(_fact(["stranded"], reason="y" * 900)), "epoch": 1}
    perform([action], deps, lambda: 1, False)
    (line,) = recorded
    assert (line["reason"], line["cause"], line["remedy"], line["command"]) == ("y" * 600, "stranded", "carry", LAND)
