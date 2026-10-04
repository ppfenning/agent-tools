"""Today's chair action counts: pure, over `chair_actions` rows."""

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta, timezone

from agent_tools.chair_exec import LAUNCH_KINDS

_LAND_KINDS = ("land", "land_phase")
_REFUSED_OR_FAILED = ("refused", "failed")


def _ts(row: Mapping[str, object]) -> datetime | None:
    """The row's ISO-8601 `ts` as an aware datetime, a naive one read as UTC; None when absent or unparseable."""
    raw = row.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def local_midnight(now: datetime, utc_offset: timedelta) -> datetime:
    """The aware instant at which `now`'s calendar day began, in the zone `utc_offset` east of UTC."""
    local = now.astimezone(timezone(utc_offset))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def chair_today(
    rows: Iterable[Mapping[str, object]], needs_chair_open: int, now: datetime, utc_offset: timedelta,
) -> dict[str, int]:
    """Counts rows by their `ts`, `kind` and `status` fields since local midnight of `now`; any refused or failed row counts."""
    start = local_midnight(now, utc_offset)
    today = [row for row in rows if (at := _ts(row)) is not None and at >= start]
    return {
        "lands": len([r for r in today if r.get("kind") in _LAND_KINDS and r.get("status") == "landed"]),
        "launches": len([r for r in today if r.get("kind") in LAUNCH_KINDS and r.get("status") == "done"]),
        "refused_or_failed": len([r for r in today if r.get("status") in _REFUSED_OR_FAILED]),
        "needs_chair_open": needs_chair_open,
    }
