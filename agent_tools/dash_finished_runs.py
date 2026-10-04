"""The newest finished runs as schema-1 run rows, with the status and last verdict a live row cannot carry."""

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

# Status names the harness already uses: `runs_top._status` gives quarantined and exited, and `chair_read_exits.row_exited`
# counts a run as exited when it has an ended_at or is quarantined. The verdict is the last `verdict` event, as `runs_top.row` reads it.
STATUSES = ("running", "approved", "landed", "quarantined", "exited")
WINDOW_HOURS = 6
LIMIT = 10


def _ended(text: Any) -> datetime | None:
    """A finish time as an aware datetime; a naive one reads as UTC, and an unparseable one is None."""
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _row(record: Mapping[str, Any]) -> dict[str, Any]:
    """The keys of `dash_feed._run_v1`, coerced the same way; a missing verdict is the empty string."""
    return {
        "run": record["run"],
        "machine": record.get("machine") or "",
        "phase": record.get("phase") or "",
        "node": record.get("node") or "",
        "attempt": int(record.get("attempt") or 0),
        "turns": int(record.get("turns") or 0),
        "cost": float(record.get("cost") or 0.0),
        "verdict": record.get("verdict") or "",
        "status": record["status"],
    }


def finished_runs(
    records: Sequence[Mapping[str, Any]],
    live_ids: Collection[str],
    now: datetime,
    window_hours: float = WINDOW_HOURS,
    limit: int = LIMIT,
) -> list[dict[str, Any]]:
    """Up to `limit` run rows, newest `ended_at` first. A record is {run, machine, phase, node, attempt, turns, cost, verdict, status, ended_at}; ended_at is ISO-8601. Live ids, rows older than the window and rows with an unknown status are dropped."""
    cutoff = now - timedelta(hours=window_hours)
    kept = [
        (ended, record)
        for record in records
        if record.get("run") not in live_ids
        and record.get("status") in STATUSES
        and (ended := _ended(record.get("ended_at"))) is not None
        and ended >= cutoff
    ]
    newest = sorted(kept, key=lambda pair: (pair[0], str(pair[1]["run"])), reverse=True)
    return [_row(record) for _, record in newest[:limit]]
