"""Plan the widen half of a chair tick: one widen_ticket per ticket stopped on a small named addition.

Pure. Takes the facts, returns actions. No I/O.
"""
from agent_tools.chair_remedy import trim_reason
from agent_tools.chair_types import Action, Facts
from agent_tools.chair_widen import classify_handoff

MAX_WIDENINGS = 2  # a ticket whose body already carries this many widening notes is left to the chair


def plan_widen(facts: Facts) -> list[Action]:
    """One widen_ticket per handoff stop that classify_handoff accepts and whose ticket has widened fewer than twice."""
    actions: list[Action] = []
    for row in facts.get("handoff_stops", []):
        pairs = None if row["widenings"] >= MAX_WIDENINGS else classify_handoff(row["reason"], row["surfaces"])
        if pairs is not None:
            actions.append(
                {
                    "kind": "widen_ticket",
                    "initiative": row["initiative"],
                    "task_id": row["task_id"],
                    "paths": [path for path, _ in pairs],
                    "additions": [addition for _, addition in pairs],
                    "reason": trim_reason(row["reason"]),
                }
            )
    return actions
