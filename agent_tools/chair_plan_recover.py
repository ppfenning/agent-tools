"""Plan the recovery half of a chair tick: relaunches, harness retries, rescues and needs-the-chair reports.

Pure. Takes the facts, returns actions. No pacing, no run history, no I/O.
"""
from typing import Literal

from agent_tools.chair_read_quarantined import RUNAWAY_CAUSE
from agent_tools.chair_types import Action, Facts, InitiativeFacts, QuarantineFacts, ReadyTask
from agent_tools.run_death_cause import SCHEMA_VERSION_CAUSE

Recovery = Literal["rescue", "retry", "needs_chair", "none"]

STRANDED_CAUSE = "stranded"


def _recovery(q: QuarantineFacts, approved: set[tuple[str, str]]) -> Recovery:
    # A runaway-ceiling quarantine always reaches the chair, ahead of every other rule: no harness_failures
    # count, retry count or approval state ever turns it into a retry or a rescue.
    if q["cause"] == RUNAWAY_CAUSE:
        return "needs_chair"
    # A stranded task that is also an approved, unlanded row waits for its own phase to land as a whole: no
    # lone land, and not the chair either. The match is (initiative, task id), not the bare id. It cannot
    # include phase: a task id repeats across phases and QuarantineFacts carries no phase, so a same-id task in
    # another phase of the same initiative still matches.
    if q["cause"] == STRANDED_CAUSE and (q["initiative"], q["task_id"]) in approved:
        return "none"
    # harness_failures == 0 on a harness cause is not a case the rules name; the chair looks at it.
    if q["cause"] != "harness" or q["harness_failures"] != 1:
        return "needs_chair"
    if q["rescue_failed"]:
        return "needs_chair"
    if q["has_patch"]:
        return "rescue"
    return "retry"


def _quarantine_action(q: QuarantineFacts, kind: Recovery) -> Action:
    if kind == "needs_chair":
        return {"kind": "needs_chair", "initiative": q["initiative"], "cause": q["cause"]}
    return {"kind": kind, "task_id": q["task_id"], "initiative": q["initiative"]}


def _quarantine_actions(quarantines: list[QuarantineFacts], approved: set[tuple[str, str]]) -> list[Action]:
    kinds = [(q, _recovery(q, approved)) for q in quarantines]
    chair = {q["initiative"] for q, kind in kinds if kind == "needs_chair"}
    return [
        _quarantine_action(q, kind)
        for q, kind in kinds
        if kind != "none" and (kind == "needs_chair" or q["initiative"] not in chair)
    ]


def _first_unmet_need(t: ReadyTask, landed: set[str]) -> str | None:
    """The first id in `t["needs"]` not in `landed`, in needs order; None when every need is landed."""
    return next((need for need in t["needs"] if need not in landed), None)


def _ready_and_met(t: ReadyTask, landed: set[str]) -> bool:
    return _first_unmet_need(t, landed) is None


def _initiative_first_unmet_need(i: InitiativeFacts) -> str | None:
    """None when a ready task has every need landed; else the first unmet need, ready tasks before waiting ones."""
    if any(_ready_and_met(t, i["landed"]) for t in i["ready_tasks"]):
        return None
    candidates = [*i["ready_tasks"], *i.get("waiting_tasks", [])]
    return next((n for t in candidates if (n := _first_unmet_need(t, i["landed"])) is not None), None)


def _can_relaunch(i: InitiativeFacts, blocked: set[str]) -> bool:
    return (
        i["started"]
        and i["id"] not in blocked
        and bool(i["ready_tasks"])
        and _initiative_first_unmet_need(i) is None
    )


def relaunch_pair(initiative: str) -> list[Action]:
    """The clear_branches then relaunch pair a healthy relaunch emits for one initiative."""
    return [{"kind": "clear_branches", "initiative": initiative}, {"kind": "relaunch", "initiative": initiative}]


def _relaunch_actions(initiatives: list[InitiativeFacts], blocked: set[str]) -> list[Action]:
    return [action for i in initiatives if _can_relaunch(i, blocked) for action in relaunch_pair(i["id"])]


def _waiting_actions(initiatives: list[InitiativeFacts], blocked: set[str]) -> list[Action]:
    """One `waiting on <need>` needs_chair per started initiative with no quarantine of its own and an unmet need."""
    return [
        {"kind": "needs_chair", "initiative": i["id"], "cause": f"waiting on {need}"}
        for i in initiatives
        if i["started"] and i["id"] not in blocked and (need := _initiative_first_unmet_need(i)) is not None
    ]


def _schema_death_actions(deaths: dict[str, list[str]]) -> list[Action]:
    """One needs_chair per initiative whose newest two runs both died on the schema-version refusal."""
    return [
        {
            "kind": "needs_chair",
            "initiative": i,
            "cause": SCHEMA_VERSION_CAUSE,
            "reason": f"the lane host's graphs is older than the shared store; runs {' and '.join(runs)} both died on a schema-version refusal",
        }
        for i, runs in deaths.items()
    ]


def _without_schema_dead_launches(actions: list[Action], deaths: dict[str, list[str]]) -> list[Action]:
    """Drop a retry or rescue for a schema-dead initiative: both start a run on the stale graphs."""
    return [a for a in actions if not (a["kind"] in {"retry", "rescue"} and a["initiative"] in deaths)]


def plan_recover(facts: Facts) -> list[Action]:
    """Schema-death reports, then quarantine actions in input order, then relaunch pairs, then waiting-on reports.

    An initiative in `schema_deaths` gets one schema-version needs_chair carrying the two run ids, and no
    relaunch, retry or rescue. Its quarantines still report their own needs_chair.

    Every open quarantine blocks its initiative's relaunch except one whose recovery is "none".
    A runaway-ceiling quarantine always reaches the chair, ahead of every other rule: it is never retried or
    rescued, whatever its harness_failures, retry count or approval state.
    A one-failure harness quarantine is rescued if it kept a patch, retried if not, and goes to the chair once a
    rescue failed. A stranded quarantine whose task is also an approved row of its initiative plans no action:
    `plan_lands` lands that task once its whole phase is done, approved or dropped, and recovery never lands it alone.
    A started initiative with a ready task but no ready task whose needs are all landed is not relaunched
    either; it reaches the chair instead, as `waiting on <need>`, naming the first unmet need.
    """
    quarantines = facts["quarantines"]
    approved = {(a["initiative"], a["id"]) for a in facts["approved"]}
    deaths = facts.get("schema_deaths", {})
    blocked = {q["initiative"] for q in quarantines if _recovery(q, approved) != "none"} | set(deaths)
    return (
        _schema_death_actions(deaths)
        + _without_schema_dead_launches(_quarantine_actions(quarantines, approved), deaths)
        + _relaunch_actions(facts["initiatives"], blocked)
        + _waiting_actions(facts["initiatives"], blocked)
    )


def plan_lost_runs(facts: Facts) -> list[Action]:
    """One mark_lost then the same clear_branches, relaunch pair, per initiative that is a key of lost_runs this tick.

    Does not check readiness, needs or run_exited: a host unreachable for ten minutes with no exit record is
    itself the evidence the previous process is gone.
    An initiative in `schema_deaths` keeps its mark_lost but gets no relaunch pair; `plan_recover` reports it.
    """
    deaths = facts.get("schema_deaths", {})
    return [
        action
        for initiative, run in facts.get("lost_runs", {}).items()
        for action in (
            {"kind": "mark_lost", "initiative": initiative, "run": run},
            *([] if initiative in deaths else relaunch_pair(initiative)),
        )
    ]
