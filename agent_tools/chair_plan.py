"""Compose one chair tick: the lease first, then lands, recovery, staleness and fill under the limits gate.

Pure. Takes the facts and the tick's clock, returns actions each stamped with the lease epoch. No I/O.
"""
from collections.abc import Sequence
from datetime import datetime, timedelta

from agent_tools import chair_login_watch, chair_plan_prune, chair_stall
from agent_tools.chair_plan_fill import HostSlot, _place_on_hosts, _required_capabilities, host_free_slots, plan_fill
from agent_tools.chair_plan_land import plan_lands
from agent_tools.chair_plan_recover import plan_lost_runs, plan_recover
from agent_tools.chair_plan_stale import plan_stale
from agent_tools.chair_types import (
    Action,
    DispatchFacts,
    Facts,
    InitiativeFacts,
    LastCall,
    LeaseFacts,
    LimitsFacts,
    StallCandidate,
    stamp,
)

_LAUNCHES = {"relaunch", "retry", "rescue"}


def _launch_cap(limits: LimitsFacts) -> int:
    return 0 if limits["go_degraded"] or limits.get("smoke_hold") is not None else max(limits["launch_cap"], 0)


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


def _cap_launches(
    actions: list[Action], cap: int, initiatives: list[InitiativeFacts], host_free: Sequence[HostSlot]
) -> tuple[list[Action], dict[str, int]]:
    """Keep the first cap relaunch, retry and rescue actions local; place relaunch and retry beyond cap on a
    lane host, ranked exactly as _place_on_hosts ranks a fresh launch_epic (weight, capabilities, free count).
    rescue never gets a host and is dropped past the cap exactly as before. What no host can take (none
    eligible, or every host lane full) is dropped as before. A dropped relaunch takes its paired
    clear_branches with it; a hosted relaunch keeps its clear_branches, which still runs locally.

    Returns the resulting actions and a mapping of host name to the lanes this placement took, for the fill
    step that follows to subtract.
    """
    launch_at = [n for n, a in enumerate(actions) if a["kind"] in _LAUNCHES]
    overflow = launch_at[cap:]
    hostable = [n for n in overflow if actions[n]["kind"] in ("relaunch", "retry")]
    by_id = {i["id"]: i for i in initiatives}
    rest = [
        (
            actions[n]["initiative"],
            _required_capabilities(by_id[actions[n]["initiative"]]) if actions[n]["initiative"] in by_id else frozenset(),
        )
        for n in hostable
    ]
    placed = iter(_place_on_hosts(rest, host_free))
    next_placed = next(placed, None)
    host_for_index: dict[int, str] = {}
    for n in hostable:
        if next_placed is not None and next_placed["initiative"] == actions[n]["initiative"]:
            host_for_index[n] = next_placed["host"]
            next_placed = next(placed, None)
    consumed: dict[str, int] = {}
    for host in host_for_index.values():
        consumed[host] = consumed.get(host, 0) + 1
    dropped = {n for n in overflow if n not in host_for_index}
    dropped_initiatives = {actions[n]["initiative"] for n in dropped if actions[n]["kind"] == "relaunch"}
    kept = [
        {**a, "host": host_for_index[n]} if n in host_for_index else a
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in dropped_initiatives)
    ]
    return kept, consumed


def _needs_chair_only(actions: list[Action]) -> list[Action]:
    return [a for a in actions if a["kind"] == "needs_chair"]


def _with_carry(action: Action, approved: list[dict]) -> Action:
    """A clear_branches gains `carry`, the sorted phases of the initiative still partial, when there are any."""
    if action["kind"] != "clear_branches":
        return action
    carry = sorted(chair_plan_prune.phases_to_carry(approved, action["initiative"]))
    return {**action, "carry": carry} if carry else action


def _dispatch_room(dispatch: DispatchFacts) -> int:
    """The lanes this machine has free before any launch this tick."""
    return max(0, dispatch["max_in_flight"] - dispatch["live_runs"])


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


def _login_needs_chair_actions(facts: Facts) -> list[Action]:
    return chair_login_watch.login_needs_chair(facts.get("login_hosts", []))


def _login_check_actions(facts: Facts, now: datetime | None) -> list[Action]:
    """One check_login per due host; requires the tick's clock, like housekeeping."""
    if now is None:
        return []
    login_hosts = facts.get("login_hosts", [])
    return [{"kind": "check_login", "host": name} for name in chair_login_watch.due_for_login_check(login_hosts, now.isoformat())]


def _stall_reason(last_call: LastCall | None, idle: float) -> str:
    minutes = round(idle)
    called = f"{last_call['role']} {last_call['task']}" if last_call is not None else "no node call yet"
    return f"{called}, idle {minutes}m"


def _stall_needs_chair(candidate: StallCandidate, idle: float) -> Action:
    return {
        "kind": "needs_chair",
        "initiative": candidate["initiative"],
        "run": candidate["run"],
        "cause": "stalled",
        "reason": _stall_reason(candidate["last_call"], idle),
    }


def plan_stall(candidates: list[StallCandidate], now: datetime) -> list[Action]:
    """stalled_usr1 for a local run's first stall, stalled_stop plus needs_chair for its second, needs_chair alone for a remote run."""
    actions: list[Action] = []
    for candidate in candidates:
        last_call = candidate["last_call"]
        idle = chair_stall.idle_minutes(last_call["ts"] if last_call is not None else None, candidate["started_at"], now)
        started = chair_stall.started_minutes(candidate["started_at"], now)
        if not chair_stall.is_stalled(idle, started):
            continue
        if not candidate["local"]:
            actions.append(_stall_needs_chair(candidate, idle))
        elif not candidate["usr1_sent"]:
            actions.append({"kind": "stalled_usr1", "run": candidate["run"], "initiative": candidate["initiative"]})
        else:
            actions.append({"kind": "stalled_stop", "run": candidate["run"], "initiative": candidate["initiative"]})
            actions.append(_stall_needs_chair(candidate, idle))
    return actions


def _plan_as_holder(facts: Facts, now: datetime | None) -> list[Action]:
    lands = plan_lands(facts)
    fetch_exits = _fetch_exit_actions(facts)
    login_needs_chair = _login_needs_chair_actions(facts)
    lost = frozenset(facts.get("lost_runs", {}))
    stale = plan_stale(facts, now) if now is not None else []
    stall = plan_stall(facts.get("stall_candidates", []), now) if now is not None else []
    remote_unfetched = frozenset(facts.get("remote_unfetched", {}))
    run_exited = facts.get("run_exited", {})
    ordinary = _withhold_lost_runs(plan_recover(facts), lost)
    pre_exit_gate = _withhold_remote_unfetched(ordinary, remote_unfetched)
    would_relaunch = frozenset(a["initiative"] for a in pre_exit_gate if a["kind"] == "relaunch")
    not_exited = frozenset(i for i in would_relaunch if not run_exited.get(i, False))
    recovered = [*_withhold_not_exited(pre_exit_gate, not_exited), *plan_lost_runs(facts)]
    if facts["limits"]["hard_stop"]:
        return [*lands, *fetch_exits, *stale, *stall, *_needs_chair_only(recovered), *login_needs_chair]
    cap = _launch_cap(facts["limits"])
    host_free = host_free_slots(facts)
    capped_actions, consumed = _cap_launches(
        recovered, min(cap, _dispatch_room(facts["dispatch"])), facts["initiatives"], host_free
    )
    capped = [_with_carry(a, facts["approved"]) for a in capped_actions]
    # A hosted relaunch or retry takes no local lane, so it must not count against the local free-lane budget.
    kept = sum(a["kind"] in _LAUNCHES and "host" not in a for a in capped)
    # Recover already owns a relaunched or quarantined initiative this tick; fill must not launch it a second time.
    # Withheld initiatives stay in the facts so their ready tasks still block a pull.
    withheld = (
        frozenset({a["initiative"] for a in capped if a["kind"] == "relaunch"} | {q["initiative"] for q in facts["quarantines"]})
        | remote_unfetched
        | not_exited
        | lost
    )
    filled = plan_fill(facts, _free_lanes(cap, kept, facts["dispatch"]), withheld, consumed)
    return [*lands, *fetch_exits, *stale, *stall, *capped, *filled, *login_needs_chair]


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
    """now is the tick's clock; without it no housekeeping or check_login is planned."""
    lease = facts["lease"]
    gated = _lease_gate(lease)
    housekeeping = plan_housekeeping(facts, now) if now is not None else []
    login_checks = _login_check_actions(facts, now)
    actions = [*_plan_as_holder(facts, now), *housekeeping, *login_checks] if gated is None else gated
    return [stamp(a, lease["epoch"]) for a in actions]
