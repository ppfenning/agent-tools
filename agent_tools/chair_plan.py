"""Compose one chair tick: the lease first, then lands, recovery and fill under the limits gate.

Pure. Takes the facts and the tick's clock, returns actions each stamped with the lease epoch. No I/O.
"""
from datetime import datetime, timedelta

from agent_tools.chair_plan_fill import plan_fill
from agent_tools.chair_plan_land import plan_lands
from agent_tools.chair_plan_recover import plan_lost_runs, plan_recover
from agent_tools.chair_types import Action, DispatchFacts, Facts, LeaseFacts, LimitsFacts, stamp

_LAUNCHES = {"relaunch", "retry", "rescue"}


def _launch_cap(limits: LimitsFacts) -> int:
    return 0 if limits["go_degraded"] else max(limits["launch_cap"], 0)


def _lease_gate(lease: LeaseFacts) -> list[Action] | None:
    """None when the lease is mine and the tick goes on; otherwise the one action the tick returns."""
    if lease["mine"]:
        return None
    if lease["released"] or lease["stale"]:
        return [{"kind": "take_lease"}]
    if lease.get("expired", False):
        return [{"kind": "take_lease", "reason": f"takeover expired at {lease.get('until', '')}"}]
    until = lease.get("until", "")
    return [{"kind": "standby", "holder": lease["holder"], "host": lease["host"], **({"until": until} if until else {})}]


def _cap_launches(actions: list[Action], cap: int) -> list[Action]:
    """Keep the first cap relaunch, retry and rescue actions; a dropped relaunch takes its paired clear_branches with it."""
    launch_at = [n for n, a in enumerate(actions) if a["kind"] in _LAUNCHES]
    dropped = set(launch_at[cap:])
    dropped_initiatives = {actions[n]["initiative"] for n in dropped if actions[n]["kind"] == "relaunch"}
    return [
        a
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in dropped_initiatives)
    ]


def _needs_chair_only(actions: list[Action]) -> list[Action]:
    return [a for a in actions if a["kind"] == "needs_chair"]


def _free_lanes(cap: int, kept: int, dispatch: DispatchFacts) -> int:
    return max(0, min(cap - kept, dispatch["max_in_flight"] - dispatch["live_runs"] - kept))


def _fetch_exit_actions(facts: Facts) -> list[Action]:
    """One fetch_exit per remote-unfetched initiative, naming its stranded run."""
    return [{"kind": "fetch_exit", "initiative": initiative, "run": run} for initiative, run in facts.get("remote_unfetched", {}).items()]


def _withhold_remote_unfetched(actions: list[Action], remote_unfetched: frozenset[str]) -> list[Action]:
    """Drop a relaunch for a remote-unfetched initiative; a dropped relaunch takes its paired clear_branches with it."""
    dropped = {n for n, a in enumerate(actions) if a["kind"] == "relaunch" and a["initiative"] in remote_unfetched}
    return [
        a
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in remote_unfetched)
    ]


def _withhold_not_exited(actions: list[Action], not_exited: frozenset[str]) -> list[Action]:
    """Drop a relaunch for an initiative whose newest run has no recorded exit; its paired clear_branches goes with it.

    A stale-looking lease is never enough on its own; only facts run_exited being true clears this gate.
    """
    dropped = {n for n, a in enumerate(actions) if a["kind"] == "relaunch" and a["initiative"] in not_exited}
    return [
        a
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in not_exited)
    ]


def _withhold_lost_runs(actions: list[Action], lost: frozenset[str]) -> list[Action]:
    """Drop a lost-run initiative's ordinary relaunch, retry and rescue: plan_lost_runs fully covers its recovery this tick.

    A dropped relaunch takes its paired clear_branches with it. needs_chair is untouched: a quarantine on an
    unrelated task of the same initiative still yields its own report as today.
    """
    dropped = {
        n
        for n, a in enumerate(actions)
        if a["initiative"] in lost and (a["kind"] == "relaunch" or a["kind"] in {"retry", "rescue"})
    }
    return [
        a
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in lost)
    ]


def _plan_as_holder(facts: Facts) -> list[Action]:
    lands = plan_lands(facts)
    fetch_exits = _fetch_exit_actions(facts)
    lost = frozenset(facts.get("lost_runs", {}))
    remote_unfetched = frozenset(facts.get("remote_unfetched", {}))
    run_exited = facts.get("run_exited", {})
    ordinary = _withhold_lost_runs(plan_recover(facts), lost)
    pre_exit_gate = _withhold_remote_unfetched(ordinary, remote_unfetched)
    would_relaunch = frozenset(a["initiative"] for a in pre_exit_gate if a["kind"] == "relaunch")
    not_exited = frozenset(i for i in would_relaunch if not run_exited.get(i, False))
    recovered = [*_withhold_not_exited(pre_exit_gate, not_exited), *plan_lost_runs(facts)]
    if facts["limits"]["hard_stop"]:
        return [*lands, *fetch_exits, *_needs_chair_only(recovered)]
    cap = _launch_cap(facts["limits"])
    capped = _cap_launches(recovered, cap)
    kept = sum(a["kind"] in _LAUNCHES for a in capped)
    # Recover already owns a relaunched or quarantined initiative this tick; fill must not launch it a second time.
    # Withheld initiatives stay in the facts so their ready tasks still block a pull.
    withheld = (
        frozenset({a["initiative"] for a in capped if a["kind"] == "relaunch"} | {q["initiative"] for q in facts["quarantines"]})
        | remote_unfetched
        | not_exited
        | lost
    )
    return [*lands, *fetch_exits, *capped, *plan_fill(facts, _free_lanes(cap, kept, facts["dispatch"]), withheld)]


def _parse_utc(ts: str | None) -> datetime | None:
    """None for an absent, unparseable or naive timestamp."""
    try:
        parsed = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def plan_housekeeping(facts: Facts, now: datetime) -> list[Action]:
    """One housekeeping action when none is recorded or the last is at least housekeeping_hours old."""
    last = _parse_utc(facts["last_housekeeping_at"])
    if last is not None and now - last < timedelta(hours=facts["housekeeping_hours"]):
        return []
    return [{"kind": "housekeeping", "reason": f"housekeeping due: last {facts['last_housekeeping_at'] if last else 'never'}"}]


def plan_tick(facts: Facts, now: datetime | None = None) -> list[Action]:
    """now is the tick's clock; without it no housekeeping is planned."""
    lease = facts["lease"]
    gated = _lease_gate(lease)
    housekeeping = plan_housekeeping(facts, now) if now is not None else []
    actions = [*_plan_as_holder(facts), *housekeeping] if gated is None else gated
    return [stamp(a, lease["epoch"]) for a in actions]
