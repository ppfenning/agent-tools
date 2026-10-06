"""CI gate verdict: pause lands when the forge is degraded or queued checks wait too long."""

from __future__ import annotations

from dataclasses import dataclass

from agent_tools.forge_status import ForgeStatus

DEFAULT_QUEUED_BOUND_SECONDS = 1800


@dataclass(frozen=True)
class CiGate:
    paused: bool
    reason: str  # incident name or queued-checks message; empty when not paused


def evaluate_gate(
    status: ForgeStatus,
    queued_since: tuple[float, ...],
    now: float,
    queued_bound_seconds: float,
) -> CiGate:
    """The status incident wins over the queued bound; an entry exactly at the bound is not stale."""
    stale = sum(1 for since in queued_since if now - since > queued_bound_seconds)
    if status.degraded:
        return CiGate(True, status.incident)
    if stale > 0:
        minutes = queued_bound_seconds / 60
        shown = int(minutes) if minutes == int(minutes) else minutes
        return CiGate(
            True,
            f"{stale} land(s) waited past the {shown}-minute queued-checks bound",
        )
    return CiGate(False, "")
