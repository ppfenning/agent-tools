"""Plan the free lanes: started epics, then unstarted ones, then intake decomposes in pairs, then a pull.

Pure. free_lanes comes from the caller; this module never reads dispatch or limits.
"""
from agent_tools.chair_types import Action, Facts


def _epic_launches(facts: Facts, free_lanes: int, withheld: frozenset[str]) -> list[Action]:
    """Started initiatives with ready tasks, then unstarted ones, each in docket order; withheld ids never launch."""
    open_ = [i for i in facts["initiatives"] if i["ready_tasks"] and i["id"] not in withheld]
    ordered = [*(i for i in open_ if i["started"]), *(i for i in open_ if not i["started"])]
    return [{"kind": "launch_epic", "initiative": i["id"]} for i in ordered[:free_lanes]]


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
    if free_lanes <= 0:
        return []
    epics = _epic_launches(facts, free_lanes, withheld)
    decomposes = _decompose_launches(facts, free_lanes - len(epics))
    lanes_left = free_lanes - len(epics) - len(decomposes)
    pulls: list[Action] = [{"kind": "pull"}] if _wants_pull(facts, lanes_left) else []
    return [*epics, *decomposes, *pulls]
