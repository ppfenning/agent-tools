"""Plan the free lanes: started epics, then unstarted ones, then intake decomposes in pairs, then a pull.

Pure. free_lanes comes from the caller; this module never reads dispatch or limits.
"""
from collections.abc import Sequence

from agent_tools.chair_types import Action, Facts


def _on_hosts(rest: list[str], host_free: Sequence[tuple[str, int]]) -> list[Action]:
    if not rest or not host_free:
        return []
    (name, free), *others = host_free
    here: list[Action] = [{"kind": "launch_epic", "initiative": i, "host": name} for i in rest[:free]]
    return [*here, *_on_hosts(rest[free:], others)]


def _epic_launches(
    facts: Facts, free_lanes: int, withheld: frozenset[str], host_free: Sequence[tuple[str, int]]
) -> list[Action]:
    """Started initiatives with ready tasks, then unstarted ones, each in docket order; withheld ids never launch.

    The first free_lanes launch locally. The rest fill host_free in order, each up to its free count.
    """
    open_ = [i for i in facts["initiatives"] if i["ready_tasks"] and i["id"] not in withheld]
    ordered = [*(i for i in open_ if i["started"]), *(i for i in open_ if not i["started"])]
    local_count = max(0, free_lanes)
    local: list[Action] = [{"kind": "launch_epic", "initiative": i["id"]} for i in ordered[:local_count]]
    return [*local, *_on_hosts([i["id"] for i in ordered[local_count:]], host_free)]


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
    host_free = [(h["name"], max(0, dispatch["max_in_flight"] - h["live_runs"])) for h in dispatch["hosts"]]
    if free_lanes <= 0 and not any(free for _, free in host_free):
        return []
    local_lanes = max(0, free_lanes)
    epics = _epic_launches(facts, free_lanes, withheld, host_free)
    local_epics = [a for a in epics if "host" not in a]
    decomposes = _decompose_launches(facts, local_lanes - len(local_epics))
    lanes_left = local_lanes - len(local_epics) - len(decomposes)
    pulls: list[Action] = [{"kind": "pull"}] if _wants_pull(facts, lanes_left) else []
    return [*epics, *decomposes, *pulls]
