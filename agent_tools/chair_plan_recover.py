"""Plan the recovery half of a chair tick: relaunches, harness retries, rescues and needs-the-chair reports.

Pure. Takes the facts, returns actions. No pacing, no run history, no I/O.
"""
from collections.abc import Mapping
from functools import reduce
from typing import Literal

from agent_tools.chair_read_quarantined import RUNAWAY_CAUSE
from agent_tools.chair_remedy import remedy_for, trim_reason
from agent_tools.chair_revert import revert_stopped
from agent_tools.chair_steer import steer_check
from agent_tools.chair_types import Action, Facts, InitiativeFacts, QuarantineFacts, ReadyTask, RunningInitiative
from agent_tools.run_death_cause import SCHEMA_VERSION_CAUSE

Recovery = Literal["rescue", "retry", "needs_chair", "relaunch", "none"]

STRANDED_CAUSE = "stranded"
MAIN_RED_CAUSE = "main_red"


def _recovery(q: QuarantineFacts, approved: set[tuple[str, str]], outcomes: Mapping[str, list[str]]) -> Recovery:
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
    # A main_red quarantine recovers by the ordinary relaunch pair until two reverts in a row. Then it plans
    # nothing and still blocks its initiative (_blocks): relaunching onto a main that keeps going red stacks work on it.
    if q["cause"] == MAIN_RED_CAUSE:
        return "none" if revert_stopped(outcomes.get(q["initiative"], [])) else "relaunch"
    # harness_failures == 0 on a harness cause is not a case the rules name; the chair looks at it.
    if q["cause"] != "harness" or q["harness_failures"] != 1:
        return "needs_chair"
    if q["rescue_failed"]:
        return "needs_chair"
    if q["has_patch"]:
        return "rescue"
    return "retry"


def _first_cause(q: QuarantineFacts) -> str:
    """The oldest attempt's cause on the current body; the fact's own cause when it lists none."""
    causes = q.get("causes") or []
    return causes[0] if causes else q["cause"]


def _quarantine_action(q: QuarantineFacts, kind: Recovery) -> Action:
    if kind == "needs_chair" and "reason" not in q:
        # A fact that carries no row reason keeps the old shape: there is nothing to record beyond the cause.
        return {"kind": "needs_chair", "initiative": q["initiative"], "cause": q["cause"]}
    if kind == "needs_chair":
        cause = _first_cause(q)
        remedy = remedy_for(cause, q.get("run", ""), q["task_id"], q.get("repo", ""))
        return {
            "kind": "needs_chair",
            "initiative": q["initiative"],
            "cause": cause,
            "reason": trim_reason(q.get("reason")),
            "remedy": remedy["kind"],
            "command": remedy["command"],
        }
    return {"kind": kind, "task_id": q["task_id"], "initiative": q["initiative"]}


def _blocks(q: QuarantineFacts, kind: Recovery) -> bool:
    """A "relaunch" recovery leaves the relaunch pair to _relaunch_actions; a stopped main_red keeps blocking it."""
    return False if kind == "relaunch" else kind != "none" or q["cause"] == MAIN_RED_CAUSE


def _quarantine_actions(
    quarantines: list[QuarantineFacts], approved: set[tuple[str, str]], outcomes: Mapping[str, list[str]]
) -> list[Action]:
    kinds = [(q, _recovery(q, approved, outcomes)) for q in quarantines]
    chair = {q["initiative"] for q, kind in kinds if kind == "needs_chair"}
    return [
        _quarantine_action(q, kind)
        for q, kind in kinds
        if kind not in ("none", "relaunch") and (kind == "needs_chair" or q["initiative"] not in chair)
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


def _landing_repos(facts: Facts) -> set[str]:
    return {landing["repo"] for landing in facts.get("landing", [])}


def _relaunch_actions(initiatives: list[InitiativeFacts], blocked: set[str], landing_repos: set[str]) -> list[Action]:
    return [
        action
        for i in initiatives
        if _can_relaunch(i, blocked) and i.get("repo") not in landing_repos
        for action in relaunch_pair(i["id"])
    ]


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


_LAUNCHES = {"relaunch", "retry", "rescue"}


def _entry(i: InitiativeFacts) -> RunningInitiative | None:
    """The initiative as a running entry; None when its repo is unknown, so there is nothing to compare."""
    repo = i.get("repo")
    return None if repo is None else {"id": i["id"], "repo": repo, "surfaces": i.get("ready_surfaces", [])}


def _steer(
    a: Action, by_id: dict[str, InitiativeFacts], running: list[RunningInitiative], streaks: Mapping[str, int]
) -> tuple[Action, RunningInitiative | None]:
    """The action to return for a launch, and the running entry it claims; a deferral claims nothing."""
    i = by_id.get(a["initiative"])
    entry = None if i is None else _entry(i)
    if entry is None:
        return a, None
    deferred = steer_check(entry["id"], entry["repo"], entry["surfaces"], running, streaks)
    return (a, entry) if deferred is None else (deferred, None)


def _without_deferred_clears(before: list[Action], after: list[Action]) -> list[Action]:
    """A deferred relaunch takes its paired clear_branches with it: the clear prunes epic/<initiative>/* branches."""
    deferred = {a["initiative"] for a in before if a["kind"] == "relaunch"} - {
        a["initiative"] for a in after if a["kind"] == "relaunch"
    }
    return [a for a in after if not (a["kind"] == "clear_branches" and a["initiative"] in deferred)]


def _steered(actions: list[Action], facts: Facts) -> list[Action]:
    """Replace each launch overlapping a running or earlier-kept initiative with its steer_check action."""
    # A retry or rescue is checked on the initiative's ready_surfaces, as the ticket specifies; the quarantined
    # task's own surfaces are not in facts. plan_lost_runs relaunches are not checked here: a follow-up item.
    by_id = {i["id"]: i for i in facts["initiatives"]}
    base = facts.get("running", [])
    streaks = facts.get("steer_streaks", {})

    def fold(
        state: tuple[list[Action], list[RunningInitiative]], a: Action
    ) -> tuple[list[Action], list[RunningInitiative]]:
        out, kept = state
        if a["kind"] not in _LAUNCHES:
            return [*out, a], kept
        action, entry = _steer(a, by_id, [*base, *kept], streaks)
        return [*out, action], kept if entry is None else [*kept, entry]

    return _without_deferred_clears(actions, reduce(fold, actions, ([], []))[0])


def claimed_by(actions: list[Action], facts: Facts) -> list[RunningInitiative]:
    """The entry of each initiative with a launch in plan_recover's `actions`, in order, one per initiative.

    It claims before the launch cap and placement run, so a launch they later drop still holds its claim this tick.
    """
    by_id = {i["id"]: i for i in facts["initiatives"]}
    entries = (
        e for a in actions if a["kind"] in _LAUNCHES and (i := by_id.get(a["initiative"])) is not None and (e := _entry(i))
    )
    return list({e["id"]: e for e in entries}.values())


def plan_recover(facts: Facts) -> list[Action]:
    """Schema-death reports, then quarantine actions in input order, then relaunch pairs, then waiting-on reports.

    A relaunch, retry or rescue that shares a surface with a running or already-kept initiative in the same repo
    is replaced by the steer_check action: a steer_clear, or needs_chair once the pair's streak reaches 3.

    An initiative in `schema_deaths` gets one schema-version needs_chair carrying the two run ids, and no
    relaunch, retry or rescue. Its quarantines still report their own needs_chair.

    An initiative whose repo has a land in `landing` gets no relaunch pair, so the pair never takes a launch slot.

    Every open quarantine blocks its initiative's relaunch except one whose recovery is "none" or "relaunch".
    A main_red quarantine recovers by relaunch until `land_outcomes` for its initiative ends in two reverts.
    Then its recovery is "none" and it keeps blocking.
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
    outcomes = facts.get("land_outcomes", {})
    blocked = {q["initiative"] for q in quarantines if _blocks(q, _recovery(q, approved, outcomes))} | set(deaths)
    return _steered(
        _schema_death_actions(deaths)
        + _without_schema_dead_launches(_quarantine_actions(quarantines, approved, outcomes), deaths)
        + _relaunch_actions(facts["initiatives"], blocked, _landing_repos(facts))
        + _waiting_actions(facts["initiatives"], blocked),
        facts,
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
