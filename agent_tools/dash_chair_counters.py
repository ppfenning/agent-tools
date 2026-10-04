"""Chair status counters for the dash feed: pure, over the records the status line reads."""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from agent_tools.chair_exec import Result
from agent_tools.chair_report import lands_this_tick
from agent_tools.dash_chair_today import _ts, local_midnight


def _age_s(stamp: str | None, now: datetime) -> int:
    """Whole seconds from an aware ISO-8601 stamp to `now`; 0 when absent, unparseable, naive or in the future."""
    try:
        then = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return 0
    if then is None or then.tzinfo is None:
        return 0
    return max(0, int((now - then).total_seconds()))


def chair_counters(
    results: Sequence[Result], action_rows: Iterable[Mapping[str, object]], last_housekeeping_at: str | None,
    now: datetime, utc_offset: timedelta,
) -> dict[str, int]:
    """`lands_today` is the status line's per-tick land count, while `today.lands` counts `chair_actions` rows since local midnight."""
    start = local_midnight(now, utc_offset)
    phases = [
        row for row in action_rows
        if row.get("kind") == "land_phase" and row.get("status") == "landed" and (at := _ts(row)) is not None and at >= start
    ]
    return {
        "lands_today": lands_this_tick(results),
        "phases_today": len(phases),
        "housekeeping_age_s": _age_s(last_housekeeping_at, now),
    }
