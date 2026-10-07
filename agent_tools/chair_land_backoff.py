"""Pure backoff for a refused land, keyed by (initiative, phase, run)."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TypedDict

BASE = timedelta(minutes=15)
CAP = timedelta(hours=2)


class LandRefusal(TypedDict):
    initiative: str
    phase: str
    run: str
    attempts: int
    last_refused_at: str  # ISO UTC


def backoff_for(attempts: int) -> timedelta:
    """15 minutes for the first refusal, doubling per attempt, never more than 2 hours."""
    return min(CAP, BASE * 2 ** max(0, attempts - 1))


def aligned(stamp: str, now: datetime) -> datetime:
    """The ISO stamp in `now`'s awareness; a naive stamp is UTC, so naive and aware never meet in a comparison."""
    parsed = datetime.fromisoformat(stamp)
    if now.tzinfo is None:
        return parsed if parsed.tzinfo is None else parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def next_attempt_at(refusal: LandRefusal) -> datetime:
    return datetime.fromisoformat(refusal["last_refused_at"]) + backoff_for(refusal["attempts"])


def suppressed(refusal: LandRefusal, now: datetime) -> bool:
    """A row missing its stamp suppresses nothing; a missing attempt count backs off as one attempt."""
    stamp = refusal.get("last_refused_at", "")
    return bool(stamp) and now < aligned(stamp, now) + backoff_for(refusal.get("attempts", 1))


def suppressing(
    refusals: Iterable[LandRefusal], initiative: str, phase: str, run: str, now: datetime
) -> LandRefusal | None:
    """The refusal still backing off this exact run; a new run id of the phase matches none."""
    return next(
        (
            r
            for r in refusals
            if (r.get("initiative"), r.get("phase"), r.get("run")) == (initiative, phase, run) and suppressed(r, now)
        ),
        None,
    )
