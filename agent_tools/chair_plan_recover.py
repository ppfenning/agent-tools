"""Plan the recovery half of a chair tick: relaunches, harness retries, rescues and needs-the-chair reports.

Pure. Takes the facts, returns actions. No pacing, no run history, no I/O.
"""
from typing import Literal

from agent_tools.chair_types import Action, Facts, InitiativeFacts, QuarantineFacts

Recovery = Literal["rescue", "retry", "needs_chair"]


def _recovery(q: QuarantineFacts) -> Recovery:
    # harness_failures == 0 on a harness cause is not a case the rules name; the chair looks at it.
    if q["cause"] != "harness" or q["harness_failures"] != 1:
        return "needs_chair"
    if q["rescue_failed"]:
        return "needs_chair"
    if q["has_patch"]:
        return "rescue"
    return "retry"


def _needs_chair_initiatives(quarantines: list[QuarantineFacts]) -> set[str]:
    return {q["initiative"] for q in quarantines if _recovery(q) == "needs_chair"}


def _quarantine_action(q: QuarantineFacts) -> Action:
    kind = _recovery(q)
    if kind == "needs_chair":
        return {"kind": "needs_chair", "initiative": q["initiative"], "cause": q["cause"]}
    return {"kind": kind, "task_id": q["task_id"], "initiative": q["initiative"]}


def _quarantine_actions(quarantines: list[QuarantineFacts]) -> list[Action]:
    chair = _needs_chair_initiatives(quarantines)
    return [
        _quarantine_action(q)
        for q in quarantines
        if _recovery(q) == "needs_chair" or q["initiative"] not in chair
    ]


def _can_relaunch(i: InitiativeFacts, blocked: set[str]) -> bool:
    return (
        i["started"]
        and i["id"] not in blocked
        and bool(i["ready_tasks"])
        and all(need in i["landed"] for t in i["ready_tasks"] for need in t["needs"])
    )


def relaunch_pair(initiative: str) -> list[Action]:
    """The clear_branches then relaunch pair a healthy relaunch emits for one initiative."""
    return [{"kind": "clear_branches", "initiative": initiative}, {"kind": "relaunch", "initiative": initiative}]


def _relaunch_actions(initiatives: list[InitiativeFacts], blocked: set[str]) -> list[Action]:
    return [action for i in initiatives if _can_relaunch(i, blocked) for action in relaunch_pair(i["id"])]


def plan_recover(facts: Facts) -> list[Action]:
    """Quarantine actions in input order, then relaunch pairs. An open quarantine blocks its initiative's relaunch.

    A one-failure harness quarantine is rescued if it kept a patch, retried if not, and goes to the chair once a rescue failed.
    """
    quarantines = facts["quarantines"]
    blocked = {q["initiative"] for q in quarantines}
    return _quarantine_actions(quarantines) + _relaunch_actions(facts["initiatives"], blocked)


def plan_lost_runs(facts: Facts) -> list[Action]:
    """One mark_lost then the same clear_branches, relaunch pair, per initiative that is a key of lost_runs this tick.

    Does not check readiness, needs or run_exited: a host unreachable for ten minutes with no exit record is
    itself the evidence the previous process is gone.
    """
    return [
        action
        for initiative, run in facts.get("lost_runs", {}).items()
        for action in ({"kind": "mark_lost", "initiative": initiative, "run": run}, *relaunch_pair(initiative))
    ]
