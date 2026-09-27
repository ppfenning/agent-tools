"""Pure rule: is a task stale, judged from board state and history signals; no file, store or clock access."""

from datetime import UTC, datetime

_NEVER_COUNTS_STATES = frozenset({"done", "dropped", "draft"})
_STALE_COUNTS_STATES = frozenset({"blocked", "ready", "approved"})
_QUARANTINE_REASON = "quarantined twice for a non-harness cause"
_NEVER = datetime.min.replace(tzinfo=UTC)


def _parse_utc(value: str | None) -> datetime:
    """`_NEVER` for a missing signal, so it never wins a `max()` against a real one."""
    if value is None:
        return _NEVER
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def stale_reason(
    state: str,
    last_file_change: str | None,
    last_run: str | None,
    last_chair_action: str | None,
    quarantine_non_harness_count: int,
    stale_days: int,
    now: str,
) -> str | None:
    """The reason a human should see this task, or None when it does not count as stale."""
    if state in _NEVER_COUNTS_STATES:
        return None
    if quarantine_non_harness_count >= 2:
        return _QUARANTINE_REASON
    if state not in _STALE_COUNTS_STATES:
        return None
    latest = max(_parse_utc(last_file_change), _parse_utc(last_run), _parse_utc(last_chair_action))
    gap_days = (_parse_utc(now) - latest).days
    return f"no file change, run, or chair action in {gap_days} days" if gap_days >= stale_days else None
