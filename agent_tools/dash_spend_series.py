"""Today's cumulative lane spend, bucketed per 10 minutes UTC. Pure: now is injected."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import accumulate, groupby

SLOT_SECONDS = 600


def _slot_start(ts: datetime, midnight: datetime) -> datetime:
    """Floor ts to the start of its 10-minute slot, counted from midnight."""
    offset = int((ts - midnight).total_seconds())
    return midnight + timedelta(seconds=offset - offset % SLOT_SECONDS)


def spend_series(
    calls: Sequence[tuple[datetime, float]], now: datetime
) -> list[list]:
    """Return [at, cumulative_cost_usd] per occupied slot of now's UTC day.

    Timestamps must be timezone-aware. `at` is the slot start as ISO-8601 UTC,
    e.g. "2026-10-04T00:10:00Z". Calls before midnight UTC or after now drop.
    """
    now_utc = now.astimezone(UTC)
    midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    in_day = [
        (ts_utc, cost)
        for ts, cost in calls
        for ts_utc in (ts.astimezone(UTC),)
        if midnight <= ts_utc <= now_utc
    ]
    slotted = sorted(
        ((_slot_start(ts, midnight), cost) for ts, cost in in_day),
        key=lambda pair: pair[0],
    )
    per_slot = [
        (slot, sum(cost for _, cost in group))
        for slot, group in groupby(slotted, key=lambda pair: pair[0])
    ]
    totals = accumulate(cost for _, cost in per_slot)
    return [
        [slot.strftime("%Y-%m-%dT%H:%M:%SZ"), total]
        for (slot, _), total in zip(per_slot, totals)
    ]
