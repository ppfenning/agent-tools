"""Pure rule: which landed main commits turned red and must be reverted; no file, store or clock access."""

from collections.abc import Sequence

from agent_tools.chair_types import Action, LandedMain


def revert_reason(m: LandedMain) -> str | None:
    """The output that justifies a revert, or None. A pending ci or smoke never counts."""
    if m["ci"] == "red":
        return m["ci_output"]
    if m["smoke"] == "failed":
        return m["smoke_output"]
    return None


def revert_check(landed: Sequence[LandedMain]) -> list[Action]:
    """One revert_land per red main, in input order."""
    return [
        Action(
            kind="revert_land",
            initiative=m["initiative"],
            phase=m["phase"],
            repo=m["repo"],
            pr=m["pr"],
            commit=m["commit"],
            reason=reason,
        )
        for m in landed
        if (reason := revert_reason(m)) is not None
    ]


def revert_stopped(outcomes: Sequence[str]) -> bool:
    """True when the last two outcomes, oldest first, are both reverted."""
    return list(outcomes[-2:]) == ["reverted", "reverted"]
