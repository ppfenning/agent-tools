"""The dash feed's chair object from the store's `leases` and `chair_actions` rows: a pure core and one thin store edge."""
from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path

from agent_tools import run_store
from agent_tools.chair import LEASE_NAME
from agent_tools.dash_chair_beat import beat_age_s
from agent_tools.dash_chair_today import chair_today, local_midnight

__all__ = ["chair_from_store", "read_chair_rows"]

# `leases` is read whole (`SELECT *`), so a store from before the `status` column simply has no tick.
# `chair_actions` rows carry kind, ts and action_json; the action's own keys (status among them) sit in action_json.


def _iso(value: object) -> object:
    """A Postgres `datetime` cell as ISO text; SQLite already hands back text, so anything else passes through."""
    return value.isoformat() if isinstance(value, datetime) else value


def _aware(value: object) -> datetime | None:
    """An aware datetime from a datetime or ISO text; None when absent, unparseable or naive."""
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None
    return at if at is not None and at.tzinfo is not None else None


def _tick_fields(status: object, now: datetime) -> dict:
    """last_tick_at, last_status, current_action and tick_age_s from the lease's `status` cell; all None unless it is a JSON object."""
    doc = run_store._json_cell(status) if status else None
    if not isinstance(doc, dict):
        return {"last_tick_at": None, "last_status": None, "current_action": None, "tick_age_s": None}
    tick_at = _iso(doc.get("tick_at"))
    action = doc.get("current_action")
    return {
        "last_tick_at": tick_at,
        "last_status": doc.get("status"),
        "current_action": action if isinstance(action, dict) else None,
        "tick_age_s": beat_age_s(tick_at, now) if isinstance(tick_at, str) else None,
    }


def _liveness(holder: str, expires_at: object, now: datetime) -> str:
    """Lease-derived, not the console row's state: `none` when released (no holder, or the 1970 sentinel expiry),
    `live` before `expires_at`, `stale` after it, `unknown` when it is absent or unparseable."""
    until = _aware(expires_at)
    if not holder or (until is not None and until.year <= 1970):
        return "none"
    if until is None:
        return "unknown"
    return "live" if until > now else "stale"


def chair_from_store(
    lease: Mapping[str, object], action_rows: Iterable[Mapping[str, object]], needs_chair_open: int,
    now: datetime, utc_offset: timedelta,
) -> dict:
    """The chair object from the chair lease row and the day's `chair_actions` rows; timestamps may be ISO text or datetimes.

    The lease holder is `label@host:pid`; `holder` is the label and `host` the part between `@` and `:`.
    """
    full = str(lease.get("holder") or "")
    label, _, rest = full.partition("@")
    heartbeat_at = _iso(lease.get("heartbeat_at"))
    age = beat_age_s(heartbeat_at, now) if isinstance(heartbeat_at, str) else None
    return {
        "holder": label,
        "host": str(lease.get("host") or rest.split(":", 1)[0]),
        "epoch": int(lease.get("epoch") or 0),
        "liveness": _liveness(full, lease.get("expires_at"), now),
        "beat_age_s": max(0, age) if age is not None else 0,
        **_tick_fields(lease.get("status"), now),
        "today": chair_today([{**row, "ts": _iso(row.get("ts"))} for row in action_rows], needs_chair_open, now, utc_offset),
    }


def _flat(row: Mapping[str, object]) -> dict:
    """A `chair_actions` row with its `action_json` keys beneath the row's own columns."""
    doc = run_store._json_cell(row.get("action_json"))
    return {**(doc if isinstance(doc, dict) else {}), **{k: v for k, v in row.items() if k != "action_json"}}


def _since(now: datetime, utc_offset: timedelta) -> str:
    """The calendar date one day before local midnight's UTC date: a prefix every ISO `ts` form (Z, offset, space) sorts after."""
    return (local_midnight(now, utc_offset) - timedelta(days=1)).date().isoformat()


def read_chair_rows(runs_dir: Path, now: datetime, utc_offset: timedelta) -> tuple[dict, list[dict]] | None:
    """Edge. The chair lease row and a superset of today's `chair_actions` rows, which `chair_from_store` narrows exactly.

    None with no store, no chair lease, or a store error; an error also prints one line to stderr so a fallback is visible.
    """
    try:
        opened = run_store._open(runs_dir)
        if opened is None:
            return None
        conn, token = opened
        try:
            lease = conn.execute(run_store._sql("SELECT * FROM leases WHERE name = {p}", token), (LEASE_NAME,)).fetchone()
            if lease is None:
                return None
            sql = run_store._sql("SELECT * FROM chair_actions WHERE ts >= {p}", token)
            rows = conn.execute(sql, (_since(now, utc_offset),)).fetchall()
            return dict(lease), [_flat(dict(r)) for r in rows]
        finally:
            conn.close()
    except (*run_store._DB_ERRORS, RuntimeError, OSError, ValueError) as err:  # an unreachable Postgres, psycopg absent, a bad store
        print(f"dash_chair_store: chair store read failed, falling back: {type(err).__name__}: {err}", file=sys.stderr)
        return None
