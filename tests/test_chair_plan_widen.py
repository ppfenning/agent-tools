from agent_tools.chair_plan_widen import plan_widen
from agent_tools.chair_types import Facts, HandoffStop


def _stop(reason: str, widenings: int = 0) -> HandoffStop:
    return {
        "initiative": "i",
        "task_id": "t1",
        "state": "handoff",
        "reason": reason,
        "surfaces": ["src/ui/regatta.rs"],
        "widenings": widenings,
    }


def _facts(*stops: HandoffStop) -> Facts:
    return {"handoff_stops": list(stops)}  # type: ignore[typeddict-item]


def test_an_accessor_only_stop_plans_one_widen_ticket():
    reason = "Needs a getter in src/app.rs for the focused frame."
    assert plan_widen(_facts(_stop(reason))) == [
        {
            "kind": "widen_ticket",
            "initiative": "i",
            "task_id": "t1",
            "paths": ["src/app.rs"],
            "additions": ["getter"],
            "reason": reason,
        }
    ]


def test_a_design_question_plans_nothing():
    assert plan_widen(_facts(_stop("Should we add a getter in src/app.rs?"))) == []


def test_three_files_outside_the_surfaces_plan_nothing():
    reason = "Needs a getter in a/x.rs. Needs a getter in b/y.rs. Needs a getter in c/z.rs."
    assert plan_widen(_facts(_stop(reason))) == []


def test_a_ticket_already_widened_twice_plans_nothing():
    assert plan_widen(_facts(_stop("Needs a getter in src/app.rs.", widenings=2))) == []


def test_the_reason_is_cut_to_600_characters():
    reason = "Needs a getter in src/app.rs. " + "x" * 700
    assert len(plan_widen(_facts(_stop(reason)))[0]["reason"]) == 600


def test_no_handoff_stops_plan_nothing():
    assert plan_widen({}) == []  # type: ignore[typeddict-item]
