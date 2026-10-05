"""Plan the outcome of a task's review PR: a merged PR is landed, a closed one goes to the chair."""
from agent_tools.chair_types import REVIEW_CLOSED_CAUSE, Action, Facts, ReviewPr


def _review_action(entry: ReviewPr) -> list[Action]:
    if entry["state"] == "merged":
        return [
            {
                "kind": "review_landed",
                "initiative": entry["initiative"],
                "phase": entry["phase"],
                "task_id": entry["task_id"],
                "repo": entry["repo"],
                "url": entry["url"],
                "merged_at": entry["merged_at"],
            }  # type: ignore[list-item]  # plan_tick stamps the epoch
        ]
    if entry["state"] == "closed":
        return [
            {
                "kind": "needs_chair",
                "initiative": entry["initiative"],
                "phase": entry["phase"],
                "task_id": entry["task_id"],
                "url": entry["url"],
                "cause": REVIEW_CLOSED_CAUSE,
            }  # type: ignore[list-item]
        ]
    return []


def plan_review(facts: Facts) -> list[Action]:
    """One review_landed per merged entry, one needs_chair per closed one; open and unknown wait."""
    return [a for entry in facts.get("review_prs", []) for a in _review_action(entry)]
