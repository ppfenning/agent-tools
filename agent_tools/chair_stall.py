"""Pure stall predicate: has a run gone quiet for STALL_MINUTES. No file, store or clock access."""
from datetime import UTC, datetime

STALL_MINUTES = 30

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _minutes_between(ts: str, now: datetime) -> float:
    then = datetime.strptime(ts, _TS_FORMAT).replace(tzinfo=UTC)
    now_aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return (now_aware - then).total_seconds() / 60


def idle_minutes(last_call_ts: str | None, started_at: str, now: datetime) -> float:
    """Minutes since the last node call, or since the run started when there has been none yet."""
    return _minutes_between(last_call_ts if last_call_ts is not None else started_at, now)


def started_minutes(started_at: str, now: datetime) -> float:
    """Minutes since the run started."""
    return _minutes_between(started_at, now)


def is_stalled(idle_minutes: float, started_minutes: float) -> bool:
    """True once both the idle gap and the run's own age clear STALL_MINUTES.

    The started_minutes guard keeps a run whose call history predates its own start
    (a fresh relaunch inheriting an old last-call timestamp) from reading as stalled.
    """
    return idle_minutes >= STALL_MINUTES and started_minutes >= STALL_MINUTES
