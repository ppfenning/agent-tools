"""Compose one chair tick: the lease first, then lands, recovery and fill under the limits gate.

Pure. Takes the facts, returns actions each stamped with the lease epoch. No I/O, no clock.
"""
from agent_tools.chair_plan_fill import plan_fill
from agent_tools.chair_plan_land import plan_lands
from agent_tools.chair_plan_recover import plan_recover
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
    return [{"kind": "standby", "holder": lease["holder"], "host": lease["host"]}]


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


def _plan_as_holder(facts: Facts) -> list[Action]:
    lands = plan_lands(facts)
    recovered = plan_recover(facts)
    if facts["limits"]["hard_stop"]:
        return [*lands, *_needs_chair_only(recovered)]
    cap = _launch_cap(facts["limits"])
    capped = _cap_launches(recovered, cap)
    kept = sum(a["kind"] in _LAUNCHES for a in capped)
    # Recover already owns a relaunched or quarantined initiative this tick; fill must not launch it a second time.
    # Withheld initiatives stay in the facts so their ready tasks still block a pull.
    withheld = frozenset({a["initiative"] for a in capped if a["kind"] == "relaunch"} | {q["initiative"] for q in facts["quarantines"]})
    return [*lands, *capped, *plan_fill(facts, _free_lanes(cap, kept, facts["dispatch"]), withheld)]


def plan_tick(facts: Facts) -> list[Action]:
    lease = facts["lease"]
    gated = _lease_gate(lease)
    actions = _plan_as_holder(facts) if gated is None else gated
    return [stamp(a, lease["epoch"]) for a in actions]
