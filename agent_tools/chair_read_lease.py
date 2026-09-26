"""The `lease` source of `chair_facts.FactsDeps`: the store row named `chair.LEASE_NAME`, or `ABSENT` when there is none."""
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_tools import chair, run_store
from agent_tools.chair import LEASE_NAME

Row = dict[str, Any]

# FactsDeps names no absent value. `{}` would read as held by nobody, and `chair_plan._lease_gate` would stand by forever.
# A lease never taken reads as released, so the gate plans `take_lease`.
ABSENT: Mapping[str, Any] = MappingProxyType({"holder": "", "host": "", "epoch": 0, "released": True, "stale": False})

# The released shape in this repo's own fixtures keeps the holder and expires the row at the Unix epoch
# (tests/test_epic_and_cli.py). A null holder is the shape of `store_cli.parse_lease`'s released reply. Both read as released.
_RELEASED_AT = datetime(1970, 1, 1, tzinfo=UTC)


def _expiry(expires_at: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _host(holder: str) -> str:
    """The host in a `session@host:pid` holder; empty for any other shape."""
    return holder.partition("@")[2].rpartition(":")[0]


def lease_record(row: Mapping[str, Any] | None, now: datetime) -> Row:
    """Stale is an expiry at or before `now`, or one that does not parse."""
    if row is None:
        return dict(ABSENT)
    holder = row["holder"] or ""
    expiry = _expiry(row["expires_at"])
    return {
        "holder": holder,
        "host": _host(holder),
        "epoch": int(row.get("epoch") or 0),
        "released": not holder or expiry == _RELEASED_AT,
        "stale": expiry is None or expiry <= now,
    }


def read_lease(runs_dir: Path, now: datetime) -> Row:
    """Edge. `ABSENT` with no store, no row, or an unreadable store."""
    # `run_store.lease` asks for a `runs:<prefix>` name, so the read reuses its private `_open`. Only holder and expires_at are
    # known columns (`run_store._lease_table` selects them); `SELECT *` keeps a missing epoch column from reading as no lease.
    opened = run_store._open(runs_dir)
    if opened is None:
        return dict(ABSENT)
    conn, mark = opened
    try:
        row = conn.execute(f"SELECT * FROM leases WHERE name = {mark}", (LEASE_NAME,)).fetchone()
    except run_store._DB_ERRORS:
        return dict(ABSENT)
    finally:
        conn.close()
    return _with_takeover(lease_record(None if row is None else dict(row), now), runs_dir, now)


def _with_takeover(lease: Row, runs_dir: Path, now: datetime) -> Row:
    """Edge. Adds `until` and `expired` from chair.json when it is this lease holder's record and carries an `until`."""
    try:
        record = chair.read(runs_dir)
    except (OSError, ValueError):
        return lease
    if record is None or "until" not in record:
        return lease
    if chair.lease_holder(str(record.get("session")), record.get("pid"), str(record.get("host"))) != lease["holder"]:
        return lease
    return {**lease, "until": str(record["until"]), "expired": chair.expired(record, now)}
