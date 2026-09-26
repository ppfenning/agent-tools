"""Drafts: initiatives whose `initiative.md` frontmatter has `draft: true`, `proposed_by` and `proposed_at`."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_tools import route

_HOUR = 3600
_DAY = 24 * _HOUR


@dataclass(frozen=True)
class DraftRow:
    id: str
    proposed_by: str
    age_seconds: int | None  # None when `proposed_at` is missing or unparseable


def _is_draft(value: object) -> bool:
    """`route.parse_frontmatter` yields the string `true`, YAML yields the bool; nothing else is a draft."""
    return value is True or value == "true"


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age_seconds(proposed_at: object, now: str) -> int | None:
    """Whole seconds from `proposed_at` to `now`, floored at 0; None when either is not an ISO timestamp."""
    start, end = _parse_utc(proposed_at), _parse_utc(now)
    return None if start is None or end is None else max(0, int((end - start).total_seconds()))


def list_drafts(frontmatters: Sequence[Mapping], now: str) -> list[DraftRow]:
    """Pure. Oldest first, then id; drafts with no age sort after every dated one."""
    rows = [
        DraftRow(
            id=str(fm.get("id", "")),
            proposed_by=str(fm.get("proposed_by") or "unknown"),
            age_seconds=_age_seconds(fm.get("proposed_at"), now),
        )
        for fm in frontmatters
        if _is_draft(fm.get("draft"))
    ]
    return sorted(rows, key=lambda r: (r.age_seconds is None, -(r.age_seconds or 0), r.id))


def format_age(seconds: int | None) -> str:
    """`?` for None, days from 24h up, hours below that, `<1h` under an hour."""
    if seconds is None:
        return "?"
    if seconds >= _DAY:
        return f"{seconds // _DAY}d"
    if seconds >= _HOUR:
        return f"{seconds // _HOUR}h"
    return "<1h"


def count_drafts(rows: Iterable[DraftRow]) -> int:
    return sum(1 for _ in rows)


def _frontmatter_of(path: Path) -> Mapping | None:
    """Edge. The initiative's frontmatter with `id` defaulted to its directory name; None when unreadable."""
    try:
        fields = route.parse_frontmatter(path.read_text(encoding="utf-8"))[0]
    except (OSError, UnicodeDecodeError):
        return None
    return {**fields, "id": fields.get("id") or path.parent.name}


def read_drafts(work_dir: Path, now: str) -> list[DraftRow]:
    """Edge. Every `<work_dir>/*/initiative.md` that can be read, handed to `list_drafts`."""
    found = (_frontmatter_of(p) for p in sorted(work_dir.glob("*/initiative.md")))
    return list_drafts([fm for fm in found if fm is not None], now)
