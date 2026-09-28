"""Plan the free lanes: started epics, then unstarted ones, then intake decomposes in pairs, then a pull.

Pure. free_lanes comes from the caller; this module never reads dispatch or limits.
"""
from collections.abc import Sequence

from agent_tools.chair_types import Action, Facts, InitiativeFacts

# name, weight (a host's share of new placements), capabilities, free lane count, assigned-so-far
HostSlot = tuple[str, int, frozenset[str], int, int]


def _required_capabilities(initiative: InitiativeFacts) -> frozenset[str]:
    return frozenset(cap for task in initiative["ready_tasks"] for cap in task.get("requires", []))


def _eligible(host: HostSlot, required: frozenset[str]) -> bool:
    _, _, capabilities, free, _ = host
    return free > 0 and required <= capabilities


def _rank(pair: tuple[int, HostSlot]) -> tuple[float, int, int]:
    """Share after one more placement, then heavier first, then list order: host order never skews the weighting."""
    idx, (_, weight, _, _, assigned) = pair
    return ((assigned + 1) / weight, -weight, idx)


def _place_on_hosts(rest: list[tuple[str, frozenset[str]]], hosts: Sequence[HostSlot]) -> list[Action]:
    """Each initiative goes to the best-ranked host whose capabilities cover its requires; none eligible, none placed."""
    if not rest:
        return []
    initiative_id, required = rest[0]
    candidates = [(idx, host) for idx, host in enumerate(hosts) if _eligible(host, required)]
    if not candidates:
        return _place_on_hosts(rest[1:], hosts)
    idx, (name, weight, capabilities, free, assigned) = min(candidates, key=_rank)
    updated: HostSlot = (name, weight, capabilities, free - 1, assigned + 1)
    new_hosts = [updated if i == idx else host for i, host in enumerate(hosts)]
    action: Action = {"kind": "launch_epic", "initiative": initiative_id, "host": name}
    return [action, *_place_on_hosts(rest[1:], new_hosts)]


def _epic_launches(
    facts: Facts, free_lanes: int, withheld: frozenset[str], host_free: Sequence[HostSlot]
) -> list[Action]:
    """Started initiatives with ready tasks, then unstarted ones, each in docket order; withheld ids never launch.

    The first free_lanes launch locally. The rest fill host_free in proportion to weight, capped at each
    host's free count, and only onto a host whose capabilities cover the initiative's ready-task requires.
    """
    open_ = [i for i in facts["initiatives"] if i["ready_tasks"] and i["id"] not in withheld]
    ordered = [*(i for i in open_ if i["started"]), *(i for i in open_ if not i["started"])]
    local_count = max(0, free_lanes)
    local: list[Action] = [{"kind": "launch_epic", "initiative": i["id"]} for i in ordered[:local_count]]
    hosted = [(i["id"], _required_capabilities(i)) for i in ordered[local_count:]]
    return [*local, *_place_on_hosts(hosted, host_free)]


def _decompose_launches(facts: Facts, lanes: int) -> list[Action]:
    """One lane per intake item, oldest first, and only an even count: a lone lane launches none."""
    n = min(lanes, len(facts["intake"]))
    return [
        {"kind": "launch_decompose", "intake_ids": [intake_id]}
        for intake_id in facts["intake"][: n - n % 2]
    ]


def _wants_pull(facts: Facts, lanes_left: int) -> bool:
    has_ready = any(i["ready_tasks"] for i in facts["initiatives"])
    return (
        lanes_left > 0
        and not facts["work_store_ready"]
        and facts["sources_configured"]
        and not has_ready
        and not facts["intake"]
    )


def plan_fill(facts: Facts, free_lanes: int, withheld: frozenset[str] = frozenset()) -> list[Action]:
    dispatch = facts["dispatch"]
    host_free: list[HostSlot] = [
        (
            h["name"],
            h.get("weight", 1),
            frozenset(h.get("capabilities", [])),
            max(0, h.get("capacity", dispatch["max_in_flight"]) - h["live_runs"]),
            0,
        )
        for h in dispatch["hosts"]
    ]
    if free_lanes <= 0 and not any(free for *_, free, _ in host_free):
        return []
    local_lanes = max(0, free_lanes)
    epics = _epic_launches(facts, free_lanes, withheld, host_free)
    local_epics = [a for a in epics if "host" not in a]
    decomposes = _decompose_launches(facts, local_lanes - len(local_epics))
    lanes_left = local_lanes - len(local_epics) - len(decomposes)
    pulls: list[Action] = [{"kind": "pull"}] if _wants_pull(facts, lanes_left) else []
    return [*epics, *decomposes, *pulls]
