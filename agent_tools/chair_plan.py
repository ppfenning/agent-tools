"""Compose one chair tick: the lease first, then lands, recovery, staleness and fill under the limits gate.

Pure. Takes the facts and the tick's clock, returns actions each stamped with the lease epoch. No I/O.
"""
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta

from agent_tools import chair_login_watch, chair_plan_prune, chair_stall
from agent_tools.chair_plan_fill import HostSlot, _place_on_hosts, _required_capabilities, host_free_slots, plan_fill
from agent_tools.chair_plan_land import fetch_action, newest_run, plan_lands
from agent_tools.chair_plan_recover import _initiative_first_unmet_need, claimed_by, plan_lost_runs, plan_recover
from agent_tools.chair_plan_review import plan_review
from agent_tools.chair_plan_stale import plan_stale
from agent_tools.chair_types import (
    Action,
    ApprovedTask,
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
_DEAD_PID_SILENCE = timedelta(minutes=10)


def _launch_cap(limits: LimitsFacts) -> int:
    return 0 if limits["go_degraded"] or limits.get("smoke_hold") is not None else max(limits["launch_cap"], 0)


def initiative_homes(newest_run_host: Mapping[str, str], unfinished: Collection[str]) -> dict[str, str]:
    """An initiative in `unfinished` (a carried partial phase or an approved task not yet landed) is homed on
    its newest run's host, "" meaning this machine; an initiative with no unfinished work has no entry."""
    return {initiative: newest_run_host[initiative] for initiative in unfinished if initiative in newest_run_host}


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


def _route_homed(
    actions: list[Action], launch_at: list[int], home: Mapping[str, str], cap: int, host_free: Sequence[HostSlot]
) -> tuple[dict[int, str], set[int], int, dict[str, int]]:
    """Route each homed relaunch, retry or rescue straight to home[initiative]: "" claims one of the cap local
    slots, a named host claims one of its free lanes by name alone, no ranking and no capability check. No
    free slot there drops the index. Returns host-by-index for the ones placed on a host, the dropped indices,
    the count of local slots this used, and the per-host lane counts consumed."""
    host_left = {name: free for name, _, _, free, _ in host_free}
    homed_at = [n for n in launch_at if actions[n]["initiative"] in home]
    host_for_index: dict[int, str] = {}
    dropped: set[int] = set()
    local_used = 0
    for n in homed_at:
        destination = home[actions[n]["initiative"]]
        if destination == "":
            if local_used < cap:
                local_used += 1
            else:
                dropped.add(n)
        elif host_left.get(destination, 0) > 0:
            host_left[destination] -= 1
            host_for_index[n] = destination
        else:
            dropped.add(n)
    consumed: dict[str, int] = {}
    for host in host_for_index.values():
        consumed[host] = consumed.get(host, 0) + 1
    return host_for_index, dropped, local_used, consumed


def _drained_hosts(login_hosts: Sequence[Mapping]) -> frozenset[str]:
    """Names of rows whose state is 'draining': out of service, so a home there stays stuck past this tick,
    unlike a merely busy `active` host whose lanes will free up on their own."""
    return frozenset(str(row.get("name") or "") for row in login_hosts if row.get("state") == "draining")


def _stuck_homes(actions: list[Action], launch_at: list[int], home: Mapping[str, str], facts: Facts) -> set[int]:
    """Indices whose home host is drained, login-lapsed, or whose lane is lost: these relocate instead of
    routing home."""
    login_hosts = facts.get("login_hosts", [])
    stuck_hosts = chair_login_watch.login_blocked(login_hosts) | _drained_hosts(login_hosts)
    lost = frozenset(facts.get("lost_runs", {}))
    return {
        n
        for n in launch_at
        if actions[n]["initiative"] in home
        and (home[actions[n]["initiative"]] in stuck_hosts or actions[n]["initiative"] in lost)
    }


def _home_fetch(initiative: str, approved: list[ApprovedTask]) -> Action | None:
    """plan_lands' fetch for the initiative's newest run among its approved rows, the run its home was placed
    by; None when no row names a run, so there is nothing to fetch the move from."""
    rows = [t for t in approved if t["initiative"] == initiative and t["run"]]
    if not rows:
        return None
    run = newest_run({t["run"] for t in rows})
    return fetch_action(run, next(t["repo"] for t in rows if t["run"] == run), initiative)


def _cap_launches(
    actions: list[Action], cap: int, initiatives: list[InitiativeFacts], host_free: Sequence[HostSlot], home: Mapping[str, str],
    facts: Facts,
) -> tuple[list[Action], dict[str, int]]:
    """An initiative in `home` routes only there: see _route_homed. A homed initiative whose host is drained,
    login-lapsed, or whose lane is lost is relocated instead: routed like a homeless relaunch or retry (ranked
    by _place_on_hosts, its own old home host excluded from the candidates, never a local lane), with a fetch
    for its newest run's branches planned right before its own first action; with no run to fetch it is dropped
    and a needs_chair names it instead. An initiative with no home, and no
    stuck home, is placed as before: the first remaining cap relaunch, retry and rescue actions local, relaunch
    and retry beyond that on a lane host, ranked exactly as _place_on_hosts ranks a fresh launch_epic (weight,
    capabilities, free count). rescue never gets a host and is dropped past the cap, or when its home is stuck,
    exactly as before. What no host can take (none eligible, or every host lane full) is dropped as before. A
    dropped relaunch takes its paired clear_branches with it; a hosted or relocated relaunch keeps its
    clear_branches. A clear for a relaunch routed to its home host names that host, so its prune and carry
    also run there; a relocated relaunch's clear names none and runs locally.

    Returns the resulting actions and a mapping of host name to the lanes this placement took, for the fill
    step that follows to subtract.
    """
    launch_at = [n for n, a in enumerate(actions) if a["kind"] in _LAUNCHES]
    stuck = _stuck_homes(actions, launch_at, home, facts)
    stuck_initiatives = {actions[n]["initiative"] for n in stuck}
    routed_home = {i: h for i, h in home.items() if i not in stuck_initiatives}
    homed_host_for_index, homed_dropped, local_used, homed_consumed = _route_homed(actions, launch_at, routed_home, cap, host_free)
    unhomed_at = [n for n in launch_at if actions[n]["initiative"] not in routed_home and n not in stuck]
    overflow = unhomed_at[max(0, cap - local_used) :]
    hostable = [n for n in overflow if actions[n]["kind"] in ("relaunch", "retry")]
    movable = {actions[n]["initiative"] for n in stuck if actions[n]["kind"] in ("relaunch", "retry")}
    fetches = {i: _home_fetch(i, facts["approved"]) for i in movable}
    unfetchable = {i for i, fetch in fetches.items() if fetch is None}
    stuck_hostable = [n for n in stuck if actions[n]["initiative"] in movable - unfetchable and actions[n]["kind"] != "rescue"]
    by_id = {i["id"]: i for i in initiatives}
    ordered = [*stuck_hostable, *hostable]
    rest = [
        (
            actions[n]["initiative"],
            _required_capabilities(by_id[actions[n]["initiative"]]) if actions[n]["initiative"] in by_id else frozenset(),
        )
        for n in ordered
    ]
    excluded = {home[actions[n]["initiative"]] for n in stuck if home.get(actions[n]["initiative"])}
    reduced_host_free = [
        (name, weight, capabilities, max(0, free - homed_consumed.get(name, 0)), assigned)
        for name, weight, capabilities, free, assigned in host_free
        if name not in excluded
    ]
    placed = iter(_place_on_hosts(rest, reduced_host_free))
    next_placed = next(placed, None)
    host_for_index: dict[int, str] = dict(homed_host_for_index)
    for n in ordered:
        if next_placed is not None and next_placed["initiative"] == actions[n]["initiative"]:
            host_for_index[n] = next_placed["host"]
            next_placed = next(placed, None)
    consumed: dict[str, int] = {}
    for host in host_for_index.values():
        consumed[host] = consumed.get(host, 0) + 1
    dropped = homed_dropped | {n for n in stuck if n not in host_for_index} | {n for n in overflow if n not in host_for_index}
    dropped_initiatives = {actions[n]["initiative"] for n in dropped if actions[n]["kind"] == "relaunch"}
    kept_at = [
        n
        for n, a in enumerate(actions)
        if n not in dropped and not (a["kind"] == "clear_branches" and a["initiative"] in dropped_initiatives)
    ]
    relocated = {actions[n]["initiative"] for n in stuck_hostable if n in host_for_index}
    # The fetch goes ahead of the moved initiative's first kept action, a mark_lost or its clear_branches.
    fetch_before = {
        min(n for n in kept_at if actions[n].get("initiative") == i): fetch
        for i, fetch in fetches.items()
        if i in relocated and fetch is not None
    }
    kept = [
        action
        for n in kept_at
        for action in (
            *([fetch_before[n]] if n in fetch_before else []),
            _hosted(actions[n], host_for_index.get(n), routed_home),
        )
    ]
    # A stuck home with no run to fetch from never moves blind: the chair hears why the relaunch was dropped.
    unmoved: list[Action] = [{"kind": "needs_chair", "initiative": i, "cause": "home_unfetchable"} for i in sorted(unfetchable)]
    return [*kept, *unmoved], consumed


def _needs_chair_only(actions: list[Action]) -> list[Action]:
    return [a for a in actions if a["kind"] == "needs_chair"]


def _hosted(action: Action, placed: str | None, routed_home: Mapping[str, str]) -> Action:
    """A launch gains the host it was placed on. A clear_branches gains its initiative's home host, so its carry
    runs where the relaunch will; a local home or a relocated initiative leaves it without one."""
    if placed is not None:
        return {**action, "host": placed}
    home = routed_home.get(action.get("initiative", ""), "")
    return {**action, "host": home} if action["kind"] == "clear_branches" and home else action


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


_HOST_BOUND_KINDS = frozenset({"fetch", "clear_branches", "land", "land_phase"})


def _withhold_blocked_hosts(actions: list[Action], facts: Facts) -> list[Action]:
    """Drop a fetch, clear_branches or land whose run or initiative is homed on a login-blocked host.

    The run is looked up in run_hosts first, then the initiative in initiative_homes. needs_chair and every other
    kind pass; the filter reads only these facts, so a host whose login_ok turns true plans again that same tick.
    """
    blocked = chair_login_watch.login_blocked(facts.get("login_hosts", []))
    run_hosts = facts.get("run_hosts", {})
    homes = facts.get("initiative_homes", {})
    return [
        a
        for a in actions
        if not (
            blocked
            and a["kind"] in _HOST_BOUND_KINDS
            and (run_hosts.get(a.get("run", "")) or homes.get(a.get("initiative", ""))) in blocked
        )
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


def _unmet_needs_ids(initiatives: list[InitiativeFacts]) -> frozenset[str]:
    """Initiatives no launch_epic may take this tick: no ready task has every need landed."""
    return frozenset(i["id"] for i in initiatives if _initiative_first_unmet_need(i) is not None)


def _steer_deferred(action: Action) -> bool:
    """A needs_chair raised once a steer pair's streak reached the ceiling."""
    return action["kind"] == "needs_chair" and action.get("cause") == "steer_deferred"


def _unstarted_waiting_chair(initiatives: list[InitiativeFacts]) -> list[Action]:
    """waiting-on reports for unstarted initiatives only; plan_recover's own waiting-on reports cover the
    started ones, so this never double-reports."""
    return [
        {"kind": "needs_chair", "initiative": i["id"], "cause": f"waiting on {need}"}
        for i in initiatives
        if not i["started"] and (need := _initiative_first_unmet_need(i)) is not None
    ]


def _login_needs_chair_actions(facts: Facts) -> list[Action]:
    return chair_login_watch.login_needs_chair(facts.get("login_hosts", []))


def _empty_decompose_needs_chair_actions(facts: Facts) -> list[Action]:
    """One needs_chair per empty_decompose entry: a decomposed intake whose ended decompose run left zero stored task items."""
    return [
        {"kind": "needs_chair", "initiative": entry["initiative"], "run": entry["run"], "cause": "empty_decompose"}
        for entry in facts.get("empty_decompose", [])
    ]


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


def _silent_dead_pid(entry: object, now: datetime) -> bool:
    """alive is False and an aware last_beat_at at least _DEAD_PID_SILENCE before now."""
    if not isinstance(entry, Mapping) or entry.get("alive") is not False:
        return False
    beat_at = entry.get("last_beat_at")
    beat = _parse_utc(beat_at) if isinstance(beat_at, str) else None
    return beat is not None and now - beat >= _DEAD_PID_SILENCE


def _dead_pid_lost(facts: Facts, now: datetime) -> dict[str, str]:
    """Initiative to its live remote run id, for a known initiative whose probe is silently dead.

    An unfetched initiative is skipped: a cleanly finished remote run also has a dead pid, and only the fetch_exit
    planned for it this tick can show whether it exited. The next tick judges it with run_exited fetched.
    """
    raw_probes = facts.get("pid_probe", {})
    probes = raw_probes if isinstance(raw_probes, Mapping) else {}
    known = {i["id"] for i in facts["initiatives"]}
    unfetched = facts.get("remote_unfetched", {})
    exited = facts.get("run_exited", {})
    remote_runs: dict[str, set[str]] = {}
    for c in facts.get("stall_candidates", []):
        if not c["local"] and c["run"]:
            remote_runs.setdefault(c["initiative"], set()).add(c["run"])
    return {
        initiative: newest_run(remote_runs[initiative])
        for initiative, entry in probes.items()
        if initiative in known
        and initiative in remote_runs
        and initiative not in unfetched
        and exited.get(initiative) is not True
        and _silent_dead_pid(entry, now)
    }


def _with_dead_pid_lost(facts: Facts, now: datetime | None) -> Facts:
    """facts with silent dead-pid initiatives merged into lost_runs; a lost_runs entry keeps its own run id."""
    if now is None:
        return facts
    return {**facts, "lost_runs": {**_dead_pid_lost(facts, now), **facts.get("lost_runs", {})}}  # type: ignore[return-value]


def _plan_as_holder(raw_facts: Facts, now: datetime | None) -> list[Action]:
    facts = _with_dead_pid_lost(raw_facts, now)
    lands = plan_lands(facts)
    fetch_exits = _fetch_exit_actions(facts)
    login_needs_chair = _login_needs_chair_actions(facts)
    empty_decompose_needs_chair = _empty_decompose_needs_chair_actions(facts)
    review = plan_review(facts)
    unstarted_waiting_chair = _unstarted_waiting_chair(facts["initiatives"])
    lost = frozenset(facts.get("lost_runs", {}))
    stale = plan_stale(facts, now) if now is not None else []
    stall = plan_stall(facts.get("stall_candidates", []), now) if now is not None else []
    remote_unfetched = frozenset(facts.get("remote_unfetched", {}))
    run_exited = facts.get("run_exited", {})
    recover_actions = plan_recover(facts)
    ordinary = _withhold_lost_runs(recover_actions, lost)
    pre_exit_gate = _withhold_remote_unfetched(ordinary, remote_unfetched)
    would_relaunch = frozenset(a["initiative"] for a in pre_exit_gate if a["kind"] == "relaunch")
    not_exited = frozenset(i for i in would_relaunch if not run_exited.get(i, False))
    recovered = [*_withhold_not_exited(pre_exit_gate, not_exited), *plan_lost_runs(facts)]
    if facts["limits"]["hard_stop"]:
        return [
            *lands,
            *fetch_exits,
            *stale,
            *stall,
            *_needs_chair_only(recovered),
            *login_needs_chair,
            *empty_decompose_needs_chair,
            *review,
            *unstarted_waiting_chair,
        ]
    cap = _launch_cap(facts["limits"])
    host_free = host_free_slots(facts)
    capped_actions, consumed = _cap_launches(
        recovered, min(cap, _dispatch_room(facts["dispatch"])), facts["initiatives"], host_free, facts.get("home", {}), facts
    )
    capped = [_with_carry(a, facts["approved"]) for a in capped_actions]
    # A hosted relaunch or retry takes no local lane, so it must not count against the local free-lane budget.
    kept = sum(a["kind"] in _LAUNCHES and "host" not in a for a in capped)
    # Recover already owns a relaunched or quarantined initiative this tick; fill must not launch it a second time.
    # Withheld initiatives stay in the facts so their ready tasks still block a pull.
    # A steer deferral already reports its initiative; fill would find the same overlap and report it twice.
    withheld = (
        frozenset({a["initiative"] for a in capped if a["kind"] == "relaunch" or a["kind"] == "steer_clear" or _steer_deferred(a)})
        | frozenset(q["initiative"] for q in facts["quarantines"])
        | remote_unfetched
        | not_exited
        | lost
        | frozenset(facts.get("schema_deaths", {}))
        | _unmet_needs_ids(facts["initiatives"])
    )
    claimed = claimed_by(recover_actions, facts)
    filled = plan_fill(facts, _free_lanes(cap, kept, facts["dispatch"]), withheld, consumed, claimed)
    return [
        *lands,
        *fetch_exits,
        *stale,
        *stall,
        *capped,
        *filled,
        *login_needs_chair,
        *empty_decompose_needs_chair,
        *review,
        *unstarted_waiting_chair,
    ]


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
    holder = _withhold_blocked_hosts(_plan_as_holder(facts, now), facts) if gated is None else []
    actions = [*holder, *housekeeping, *login_checks] if gated is None else gated
    return [stamp(a, lease["epoch"]) for a in actions]
