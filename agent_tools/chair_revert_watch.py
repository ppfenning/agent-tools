"""Lands waiting to be judged, kept as `chair_actions` rows: a `revert_watch` per land, a `revert_watch_resolved` per verdict.

The recorder appends, so the store holds one row per `record_watch` call. `watches` is the one-per-commit view; every
reader goes through it rather than repeating the dedupe.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from agent_tools.chair_types import LandWatch

Row = Mapping[str, Any]
Write = Callable[[Mapping[str, Any]], None]  # the chair's action recorder, `chair_read_record.recorder(...)`
Read = Callable[[], Sequence[Row]]  # the `chair_actions` rows, as `chair_facts.FactsDeps.actions` takes them

WATCH_KIND = "revert_watch"
RESOLVED_KIND = "revert_watch_resolved"
OUTCOMES = ("reverted", "held")
# The store's record-action refuses a line without ts, epoch, kind and status; the recorder adds ts and epoch.
STATUS = "recorded"
_NEVER = datetime.min.replace(tzinfo=UTC)


def _doc(row: Row) -> Row:
    """The `action_json` keys beneath the row's own columns, so the store's `ts` column wins over the line's."""
    raw = row.get("action_json")
    doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    return {**(doc if isinstance(doc, dict) else {}), **{k: v for k, v in row.items() if k != "action_json"}}


def _docs(read: Read, kind: str) -> list[Row]:
    return [d for d in map(_doc, read()) if d.get("kind") == kind]


def _parsed(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if raw:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return _NEVER


def _when(doc: Row) -> datetime:
    """A Postgres datetime or an ISO string with `Z`, as aware UTC; a row with no ts sorts first."""
    parsed = _parsed(doc.get("ts"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _watch(doc: Row) -> LandWatch:
    return LandWatch(
        initiative=doc["initiative"], phase=doc["phase"], repo=doc["repo"], pr=doc["pr"], commit=doc["commit"]
    )


def record_watch(write: Write, watch: LandWatch) -> None:
    write(
        {
            "kind": WATCH_KIND,
            "status": STATUS,
            "initiative": watch["initiative"],
            "phase": watch["phase"],
            "repo": watch["repo"],
            "pr": watch["pr"],
            "commit": watch["commit"],
        }
    )


def resolve_watch(write: Write, commit: str, outcome: str) -> None:
    """Outcome is `reverted` or `held`; the recorder's `ts` is the resolution time `outcomes` orders by."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, not {outcome!r}")
    write({"kind": RESOLVED_KIND, "status": STATUS, "commit": commit, "outcome": outcome})


def watches(read: Read) -> dict[str, LandWatch]:
    """Commit to its watch, one per commit however many times it was recorded, in first-recorded order."""
    return {d["commit"]: _watch(d) for d in _docs(read, WATCH_KIND)}


def pending_watches(read: Read) -> list[LandWatch]:
    resolved = {d["commit"] for d in _docs(read, RESOLVED_KIND)}
    return [w for c, w in watches(read).items() if c not in resolved]


def outcomes(read: Read) -> dict[str, list[str]]:
    """Oldest resolution first; a commit counts once, at its earliest verdict; equal times order by commit."""
    watched = watches(read)
    by_time = sorted(_docs(read, RESOLVED_KIND), key=lambda d: (_when(d), d["commit"]))
    earliest = {d["commit"]: d for d in reversed(by_time) if d["commit"] in watched}
    verdicts = sorted(earliest.values(), key=lambda d: (_when(d), d["commit"]))
    initiatives = dict.fromkeys(watched[d["commit"]]["initiative"] for d in verdicts)
    return {i: [d["outcome"] for d in verdicts if watched[d["commit"]]["initiative"] == i] for i in initiatives}
