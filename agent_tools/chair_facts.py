"""The edge of a chair tick: gather `chair_types.Facts` from injected sources.

The shapers are pure and `gather_facts` calls each source once. The only clock is the `now` argument.
No retry state is stored: `harness_failures` is counted from run history on every call.
"""
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_tools import pacing
from agent_tools.chair import lease_holder
from agent_tools.chair_plan_land import planned_tasks
from agent_tools.chair_types import (
    ApprovedTask,
    DispatchFacts,
    Facts,
    InitiativeFacts,
    LeaseFacts,
    LimitsFacts,
    QuarantineFacts,
)

HARNESS_CAUSE = "harness"
RESCUE_FAILED_CAUSE = "rescue_failed"
STRANDED_CAUSE = "stranded"
LAUNCHING_VERDICTS = frozenset({"go", "go_degraded"})

Row = Mapping[str, Any]


@dataclass(frozen=True)
class FactsDeps:
    """One callable per source, so a test passes fakes. Each field names the row keys its source returns.

    lease: the store lease record, keys holder, host, epoch, released, stale.
    docket: the `route context` docket, keys initiatives (id, started, ready_tasks, landed), busy_lanes, max_in_flight.
    approved: approved tasks, keys id, initiative, repo, phase, phase_done, needs, run (empty when no record is stranded).
    quarantined: open quarantined work items, keys initiative, phase, task, from each item's work-store path.
    stranded: `runs_stranded.stranded` rows as they land, keys run, task, phase, branch, remedy. No initiative.
    attempts: every attempt over all runs, oldest first, keys run, phase, task, cause, and initiative when known.
        A work item's frontmatter `attempts` entries carry run and cause, as `stats_chair` reads them; the item's
        path adds phase and task. The store's `attempts` table has no task column (`run_store.attempt_causes`).
    has_patch: (initiative, task id) to whether the task's stored record holds a non-blank build.patch. Called once
        per quarantine entry, and False when no record can be found.
    live_initiatives: initiatives with a live run, meaning the `runs:<initiative>` store lease is held and
        unexpired, or failing that `runs/<run>.pid` names a live pid.
    """

    lease: Callable[[], Row]
    window: Callable[[], pacing.Window]
    weekly: Callable[[], pacing.Window | None]
    policy: Callable[[], pacing.Policy]
    docket: Callable[[], Row]
    approved: Callable[[], Sequence[Row]]
    quarantined: Callable[[], Sequence[Row]]
    stranded: Callable[[], Sequence[Row]]
    attempts: Callable[[], Sequence[Row]]
    has_patch: Callable[[str, str], bool]
    live_initiatives: Callable[[], Collection[str]]
    intake: Callable[[], Sequence[str]]
    work_store_ready: Callable[[], bool]
    sources_configured: Callable[[], bool]
    session: str
    pid: int
    host: str


def lease_facts(record: Row, session: str, pid: int, host: str) -> LeaseFacts:
    """`mine` needs this holder on a lease that is neither released nor stale."""
    holder = str(record.get("holder") or "")
    released = bool(record.get("released", False))
    stale = bool(record.get("stale", False))
    return {
        "holder": holder,
        "host": str(record.get("host") or host),
        "epoch": int(record.get("epoch") or 0),
        "mine": holder == lease_holder(session, pid, host) and not released and not stale,
        "released": released,
        "stale": stale,
    }


def weekly_fraction(weekly: pacing.Window | None) -> float:
    """Spent over ceiling. No weekly window, or one with no usable ceiling, is unguarded and reads 0.0, as in pacing.assess."""
    if weekly is None or weekly.ceiling_usd is None or weekly.ceiling_usd <= 0:
        return 0.0
    return weekly.spent_usd / weekly.ceiling_usd


def limits_facts(
    assessment: pacing.Assessment, policy: pacing.Policy, weekly: pacing.Window | None, max_in_flight: int
) -> LimitsFacts:
    """`launch_cap` is the whole lane budget on a launching verdict and none on `hold` or `stop`."""
    fraction = weekly_fraction(weekly)
    return {
        "hard_stop": fraction >= policy.weekly_hard_stop_fraction,
        "weekly_fraction": fraction,
        "hard_stop_fraction": policy.weekly_hard_stop_fraction,
        "launch_cap": max_in_flight if assessment.verdict in LAUNCHING_VERDICTS else 0,
        "go_degraded": assessment.verdict == "go_degraded",
        "five_hour_fraction": assessment.spent_fraction,
    }


Key = tuple[str, str, str]


def run_initiative(run: str) -> str:
    """The initiative a run id belongs to: `x-3` is `x`, as the `runs:<initiative>` store lease is named."""
    return re.sub(r"-\d+$", "", run)


def _key(row: Row) -> Key:
    """(initiative, phase, task). A task id repeats across phases, so phase is part of the key.

    A row with no initiative, such as every `runs_stranded` row, takes it from its run id."""
    initiative = str(row.get("initiative") or run_initiative(str(row.get("run") or "")))
    return initiative, str(row.get("phase") or ""), str(row.get("task") or "")


def harness_failures(attempts: Sequence[Row], key: Key) -> int:
    """The task's harness-cause attempts over all runs. A retry that fails again reads one higher."""
    return sum(1 for a in attempts if _key(a) == key and a.get("cause") == HARNESS_CAUSE)


def rescue_failed(attempts: Sequence[Row], key: Key) -> bool:
    """Whether the task has a rescue_failed attempt, over the same attempts and key `harness_failures` counts."""
    return any(_key(a) == key and a.get("cause") == RESCUE_FAILED_CAUSE for a in attempts)


def newest_cause(attempts: Sequence[Row], key: Key) -> str:
    """The cause on the task's newest attempt, from the list `harness_failures` counts. Empty with no attempt."""
    return next((str(a.get("cause") or "") for a in reversed(attempts) if _key(a) == key), "")


def _quarantine(
    key: Key, cause: str, attempts: Sequence[Row], has_patch: Callable[[str, str], bool]
) -> QuarantineFacts:
    return {
        "task_id": key[2],
        "initiative": key[0],
        "cause": cause,
        "harness_failures": harness_failures(attempts, key),
        "has_patch": has_patch(key[0], key[2]),
        "rescue_failed": rescue_failed(attempts, key),
    }


def quarantine_facts(
    quarantined: Sequence[Row],
    stranded: Sequence[Row],
    attempts: Sequence[Row],
    live: Collection[str],
    has_patch: Callable[[str, str], bool],
    landing: Sequence[ApprovedTask] = (),
) -> list[QuarantineFacts]:
    """One entry per open quarantine with its newest cause, then the stranded rows those did not name.

    `landing` is what `chair_plan_land.planned_tasks` plans this tick. A stranded row for one of those tasks
    that carries a run is left out, since its land handles it; a land that then fails raises its own needs_chair.

    An initiative with a live run contributes nothing: a retry in flight is never planned again."""
    opened = list(dict.fromkeys(_key(q) for q in quarantined))
    landed_now = {(t["initiative"], t["phase"], t["id"]) for t in landing if t["run"]}
    # A stranded task that is also an open quarantine keeps the quarantine's newest cause, so it is not sent to the chair as stranded.
    stuck = [k for k in dict.fromkeys(_key(s) for s in stranded) if k not in opened and k not in landed_now]
    return [
        *(_quarantine(k, newest_cause(attempts, k), attempts, has_patch) for k in opened if k[0] not in live),
        *(_quarantine(k, STRANDED_CAUSE, attempts, has_patch) for k in stuck if k[0] not in live),
    ]


def initiative_facts(docket: Row, live: Collection[str]) -> list[InitiativeFacts]:
    """The docket's initiatives with no live run, so a live run is never relaunched."""
    return [
        {
            "id": i["id"],
            "started": bool(i["started"]),
            "ready_tasks": [{"id": t["id"], "needs": list(t["needs"])} for t in i["ready_tasks"]],
            "landed": set(i["landed"]),
        }
        for i in docket["initiatives"]
        if i["id"] not in live
    ]


def dispatch_facts(docket: Row, lane_hosts: Sequence[str], live_by_host: Mapping[str, int]) -> DispatchFacts:
    """live_by_host maps a host name to its live lanes; the local machine is under the empty name."""
    return {
        "max_in_flight": int(docket["max_in_flight"]),
        "live_runs": live_by_host.get("", 0),
        "hosts": [{"name": name, "live_runs": live_by_host.get(name, 0)} for name in lane_hosts],
    }


def approved_facts(rows: Sequence[Row]) -> list[ApprovedTask]:
    return [
        {
            "id": r["id"],
            "initiative": r["initiative"],
            "repo": r["repo"],
            "phase": str(r["phase"]),
            "phase_done": bool(r["phase_done"]),
            "needs": list(r["needs"]),
            "run": str(r["run"]),
        }
        for r in rows
    ]


def gather_facts(deps: FactsDeps, now: datetime) -> Facts:
    policy = deps.policy()
    weekly = deps.weekly()
    assessment = pacing.assess(deps.window(), policy, now, weekly)
    docket = deps.docket()
    # No source reports lanes per host yet: every live lane counts as local until one does.
    dispatch = dispatch_facts(docket, [], {"": int(docket["busy_lanes"])})
    live = set(deps.live_initiatives())
    approved = approved_facts(deps.approved())
    initiatives = initiative_facts(docket, live)
    return {
        "lease": lease_facts(deps.lease(), deps.session, deps.pid, deps.host),
        "limits": limits_facts(assessment, policy, weekly, dispatch["max_in_flight"]),
        "dispatch": dispatch,
        "approved": approved,
        "initiatives": initiatives,
        "quarantines": quarantine_facts(
            deps.quarantined(), deps.stranded(), deps.attempts(), live, deps.has_patch, planned_tasks(approved, initiatives)
        ),
        "intake": list(deps.intake()),
        "work_store_ready": deps.work_store_ready(),
        "sources_configured": deps.sources_configured(),
    }
