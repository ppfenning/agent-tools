"""The chair's beat age and last status line for the dash feed: a pure core and one thin file edge."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

__all__ = ["beat_age_s", "parse_status_line", "read_status_record", "status_from_store"]

# `format_status` and `error_line` both open a line with `chair MM-DD HH:MM <TZ>`, Eastern, no year.
_STATUS = re.compile(r"chair (\d{2}-\d{2} \d{2}:\d{2} [A-Z]{2,5})(?: \||$)")


def beat_age_s(heartbeat_at: str | None, now: datetime) -> int | None:
    """Whole seconds from `heartbeat_at` to `now`; None when missing, unparseable or naive. Both must be UTC-aware."""
    try:
        then = datetime.fromisoformat(heartbeat_at) if heartbeat_at else None
    except ValueError:
        return None
    if then is None or then.tzinfo is None:  # a naive stamp cannot be subtracted from the aware `now`
        return None
    return int((now - then).total_seconds())


def parse_status_line(text: str | None) -> tuple[str | None, str | None]:
    """(last_tick_at, last_status) from the last non-blank line; the stamp is as written, Eastern with no year."""
    # No writer produces a parsed dict for the status line, so the record is text only.
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    match = _STATUS.match(lines[-1]) if lines else None
    return (match.group(1), lines[-1]) if match else (None, None)


def status_from_store(doc: dict | None) -> tuple[str | None, str | None]:
    """(last_tick_at, last_status) from a store `status` row's `line`; (None, None) when the row or its line is missing."""
    line = doc.get("line") if doc else None
    return parse_status_line(line) if isinstance(line, str) else (None, None)


def read_status_record(path: Path) -> tuple[str | None, str | None]:
    """Edge. Read `path` and parse it; (None, None) when unreadable.

    The status line is stdout redirected to a log, not a record, so the path comes from the caller.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (None, None)
    return parse_status_line(text)
