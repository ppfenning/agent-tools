"""Pure rebase_phase planning: no clock, no I/O, no git."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from agent_tools.chair_types import Action, PhaseBranch


def backup_ref(branch: str, tip: str) -> str:
    """The ref that preserves the replaced tip: the first 8 characters of the tip."""
    return f"backup/{branch}-{tip[:8]}"


def plan_rebase(branches: Sequence[PhaseBranch], relaunching: Collection[str], base: str = "main") -> list[Action]:
    """One rebase_phase per relaunching branch that is behind and carries its own commits (ahead), sorted by initiative then phase."""
    wanted = [b for b in branches if b["initiative"] in relaunching and b["behind"] > 0 and b["ahead"] > 0]
    return [
        Action(
            kind="rebase_phase",
            initiative=b["initiative"],
            phase=b["phase"],
            branch=b["branch"],
            tip=b["tip"],
            base=base,
        )
        for b in sorted(wanted, key=lambda b: (b["initiative"], b["phase"]))
    ]
