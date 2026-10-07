"""Pure launch budget rule: how often and how repeatedly an initiative may launch."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

WINDOW = timedelta(hours=1)
DEFAULT_MAX_LAUNCHES_PER_HOUR = 4


@dataclass(frozen=True)
class Launch:
    at: datetime
    kind: str  # "launch_epic" or "relaunch"


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    quarantined: bool
    reason: str
    body: str
    main_head: str


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""
    kind: str = ""  # "" when allowed, else "budget" or "relaunch_loop"
    run_ids: tuple[str, ...] = ()


ALLOWED = Verdict(allowed=True)


def normalize_reason(reason: str) -> str:
    return " ".join(reason.casefold().split())


def _launches_in_window(now: datetime, launches: Sequence[Launch]) -> int:
    return sum(1 for launch in launches if now - WINDOW < launch.at <= now)


def _relaunch_loop(outcomes: Sequence[RunOutcome], body: str, main_head: str) -> Verdict | None:
    """Outcomes arrive newest first; only the two newest are compared."""
    if len(outcomes) < 2:
        return None
    newest, previous = outcomes[0], outcomes[1]
    reason = normalize_reason(newest.reason)
    same_inputs = all(run.body == body and run.main_head == main_head for run in (newest, previous))
    looping = (
        newest.quarantined
        and previous.quarantined
        and reason != ""
        and reason == normalize_reason(previous.reason)
        and same_inputs
    )
    if looping:
        return Verdict(
            allowed=False,
            reason=(f"relaunch loop: runs {newest.run_id} and {previous.run_id} were both quarantined with '{reason}'"),
            kind="relaunch_loop",
            run_ids=(newest.run_id, previous.run_id),
        )
    return None


def launch_budget(
    now: datetime,
    kind: str,
    launches: Sequence[Launch],
    outcomes: Sequence[RunOutcome],
    body: str,
    main_head: str,
    max_launches_per_hour: int = DEFAULT_MAX_LAUNCHES_PER_HOUR,
) -> Verdict:
    """Decide whether an initiative may launch now; kind is launch_epic or relaunch."""
    count = _launches_in_window(now, launches)
    if count >= max_launches_per_hour:
        return Verdict(
            allowed=False,
            reason=(
                f"launch budget: {count} launches in the last hour (max_launches_per_hour={max_launches_per_hour})"
            ),
            kind="budget",
        )
    loop = _relaunch_loop(outcomes, body, main_head) if kind == "relaunch" else None
    return loop if loop is not None else ALLOWED
