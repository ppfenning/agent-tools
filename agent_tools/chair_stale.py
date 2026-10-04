"""Pure rule: is a task stale, judged from board state and history signals; no file, store or clock access."""

from datetime import UTC, datetime

_NEVER_COUNTS_STATES = frozenset({"done", "dropped", "draft"})
_STALE_COUNTS_STATES = frozenset({"blocked", "ready", "approved"})
_QUARANTINE_REASON = "quarantined twice for a non-harness cause"
_NO_SIGNAL_REASON = "no activity recorded"


def _parse_utc(value: str) -> datetime:
    """A naive timestamp is read as UTC."""
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
    signals = [_parse_utc(value) for value in (last_file_change, last_run, last_chair_action) if value is not None]
    if not signals:
        return _NO_SIGNAL_REASON
    gap_days = (_parse_utc(now) - max(signals)).days
    return f"no file change, run, or chair action in {gap_days} days" if gap_days >= stale_days else None
