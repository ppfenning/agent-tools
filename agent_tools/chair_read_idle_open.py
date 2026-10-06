"""The open idle-stall item: `open_signature` and `open_diagnosis` of `IdleStallInputs`.

An item is open exactly as the dashboard's open needs-chair count decides it: an unacknowledged courier entry with a
`coxswain://needs_chair/<id>` ref (`dash_feed.open_needs_chair_ids`). The `<id>` of an idle-stall item is the
signature the planner recorded, so a `chair_actions` row is open when its signature is among those ids.
unknown: nothing writes that courier entry yet, so until a writer sends one with the signature as id, no stall reads open.
A row's `signature` and `reason` sit beside `kind` and `cause`, or inside `action_json` as the store keeps them.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import courier
from agent_tools.chair_read_stale import read_chair_actions
from agent_tools.dash_chair_today import _ts
from agent_tools.dash_feed import _courier_blob, _iso_ts, open_needs_chair_ids

CAUSE = "idle_stall"
_OLDEST = datetime.min.replace(tzinfo=UTC)


def _fields(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """The row with its `action_json` fields laid over it; a Postgres dict or a SQLite JSON string, else nothing."""
    raw = row.get("action_json")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        parsed = None
    return {**row, **parsed} if isinstance(parsed, dict) else row


def open_idle_stall(
    rows: Sequence[Mapping[str, Any]], open_items: Collection[str]
) -> tuple[str | None, str | None]:
    """(signature, reason) of the newest open `idle_stall` needs_chair row, else (None, None); a resolved one is not open."""
    flat = [_fields(_iso_ts(dict(r))) for r in rows]
    mine = [
        r for r in flat
        if r.get("kind") == "needs_chair" and r.get("cause") == CAUSE and r.get("signature") in open_items
    ]
    newest = max(enumerate(mine), key=lambda p: (_ts(p[1]) or _OLDEST, p[0]), default=None)
    return (str(newest[1]["signature"]), str(newest[1].get("reason") or "")) if newest is not None else (None, None)


def read_idle_open(ws: Path) -> tuple[str | None, str | None]:
    """Edge. Never raises: no store, no table, an unreadable courier file or a database error gives (None, None)."""
    try:
        rows = read_chair_actions(ws / "runs")
        return open_idle_stall(rows, open_needs_chair_ids(courier.inbox(_courier_blob(ws))))
    except Exception:  # a tick must survive any unreadable source
        return (None, None)
