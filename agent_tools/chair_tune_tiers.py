"""Pure rule: propose a cheaper tier for a role only when first-try approval stays within a bound; no I/O, no clock.

Input rows are `TierRow` values, one per role, tier and model over the last 7 days. The later reader edge fills
them from `cox stats models`, `cox stats tiers` and `cox stats gates`. Rows arrive already windowed.

- role: the role name, such as "builder".
- tier: the tier name the row ran at.
- model: the model name the row ran on.
- runs: how many runs the row covers.
- approval_pct: first-try approval rate, in percent (0 to 100).
- cost_per_approved: cost per approved run, in the stats cost unit.
- settings_key: the key that sets this role's tier or model, as passed to `cox settings set`.
- current: True on the one row that is the role's configured tier and model.

A role with no current row, or with more than one, gets no proposal.

The command value is the target row's tier when its tier differs from the current row's, otherwise its model.
This module only builds the command string. It never applies it.
"""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class TierRow:
    role: str
    tier: str
    model: str
    runs: int
    approval_pct: float
    cost_per_approved: float
    settings_key: str
    current: bool = False


@dataclass(frozen=True)
class TierProposal:
    role: str
    from_tier: str
    from_model: str
    to_tier: str
    to_model: str
    from_approval_pct: float
    to_approval_pct: float
    from_cost: float
    to_cost: float
    from_runs: int
    to_runs: int
    command: str


def _command(current: TierRow, target: TierRow) -> str:
    value = target.tier if target.tier != current.tier else target.model
    return f"cox settings set {target.settings_key} {value}"


def _proposal(current: TierRow, target: TierRow) -> TierProposal:
    return TierProposal(
        role=current.role,
        from_tier=current.tier,
        from_model=current.model,
        to_tier=target.tier,
        to_model=target.model,
        from_approval_pct=current.approval_pct,
        to_approval_pct=target.approval_pct,
        from_cost=current.cost_per_approved,
        to_cost=target.cost_per_approved,
        from_runs=current.runs,
        to_runs=target.runs,
        command=_command(current, target),
    )


def _role_proposal(rows: list[TierRow], bound_points: float, min_runs: int) -> TierProposal | None:
    currents = [row for row in rows if row.current]
    if len(currents) != 1:
        return None
    current = currents[0]
    candidates = [
        row
        for row in rows
        if not row.current
        and row.cost_per_approved < current.cost_per_approved
        and row.runs >= min_runs
        and current.approval_pct - row.approval_pct <= bound_points
    ]
    if not candidates:
        return None
    return _proposal(current, min(candidates, key=lambda row: (row.cost_per_approved, row.tier, row.model)))


def tier_proposals(rows: Iterable[TierRow], bound_points: float = 5.0, min_runs: int = 10) -> list[TierProposal]:
    """The cheapest qualifying row per role, ordered by role. A drop of exactly bound_points qualifies."""
    by_role: dict[str, list[TierRow]] = {}
    for row in rows:
        by_role.setdefault(row.role, []).append(row)
    proposals = (_role_proposal(by_role[role], bound_points, min_runs) for role in sorted(by_role))
    return [proposal for proposal in proposals if proposal is not None]


def _render_one(proposal: TierProposal) -> str:
    return "\n".join(
        [
            f"{proposal.role}: {proposal.from_tier}/{proposal.from_model} -> {proposal.to_tier}/{proposal.to_model}",
            f"  approval: {proposal.from_approval_pct:.1f}% -> {proposal.to_approval_pct:.1f}%",
            f"  cost per approved run: {proposal.from_cost:.2f} -> {proposal.to_cost:.2f}",
            f"  runs: {proposal.from_runs} -> {proposal.to_runs}",
            f"  command: {proposal.command}",
        ]
    )


def render_proposals(proposals: list[TierProposal]) -> str:
    """One text body with every proposal, its evidence and its command. Empty when there are none."""
    return "\n\n".join(_render_one(proposal) for proposal in proposals)
