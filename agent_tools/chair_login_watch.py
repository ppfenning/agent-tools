"""Pure login-watch rules: which active hosts are due for a `check_login`, which are blocked on a
lapsed login, the `needs_chair` line that names them, and the version-merge that records a check's
result. No I/O, no ssh, no store call: everything here operates on the row shape `run_store.hosts`
returns and `chair_read_lost.stale_hosts` already reads: `name`, `ssh`, `capacity`, `state` (active,
draining, offline), `beat_at`, `versions_json` (cox, graphs, cartridges, claude versions, `login_ok`,
and now `login_checked_at`), `updated_at`, `updated_by`. The edge that calls these against the real
store, performs the ssh check, and writes the result back is a separate module. `versions_json` is a
JSON string on SQLite and a mapping on Postgres; `_versions` below decodes both, independently of
`host_cmd`'s own private helper of the same purpose, so this module carries no dependency on it.
"""
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

Row = Mapping[str, Any]

DEFAULT_THRESHOLD_S = 1800


def _versions(row: Row) -> dict:
    """`versions_json` as a dict: a JSON string from SQLite, already a mapping from a Postgres JSONB
    column."""
    raw = row.get("versions_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parsed(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def due_for_login_check(hosts: Sequence[Row], now: str, threshold_s: int = DEFAULT_THRESHOLD_S) -> list[str]:
    """Names of `active` rows whose `versions_json` has no `login_checked_at`, or one more than
    `threshold_s` seconds before `now`."""
    now_parsed = _parsed(now) or datetime.min.replace(tzinfo=UTC)
    return [
        str(row.get("name") or "")
        for row in hosts
        if row.get("state") == "active" and _checked_long_ago(_versions(row).get("login_checked_at"), now_parsed, threshold_s)
    ]


def _checked_long_ago(checked_at: object, now: datetime, threshold_s: int) -> bool:
    """True when `checked_at` is blank, unparsable, or more than `threshold_s` seconds before `now`."""
    parsed = _parsed(checked_at) if checked_at else None
    return parsed is None or (now - parsed).total_seconds() > threshold_s


def login_blocked(hosts: Sequence[Row]) -> set[str]:
    """Names of `active` rows whose `versions_json` `login_ok` is `False` exactly. Missing or `None`
    is not blocked: a host never checked has not failed."""
    return {
        str(row.get("name") or "")
        for row in hosts
        if row.get("state") == "active" and _versions(row).get("login_ok") is False
    }


def login_needs_chair(hosts: Sequence[Row]) -> list[dict]:
    """One `{"kind": "needs_chair", "host": name, "cause": "login_lapsed"}` action per name in
    `login_blocked(hosts)`, never more than one per host."""
    return [{"kind": "needs_chair", "host": name, "cause": "login_lapsed"} for name in sorted(login_blocked(hosts))]


def merge_login_versions(versions: Mapping[str, Any], login_ok: bool, checked_at: str) -> dict:
    """A new dict carrying every key of `versions` plus `login_ok` and `login_checked_at` set to the
    given values. `versions` itself is never mutated."""
    return {**versions, "login_ok": login_ok, "login_checked_at": checked_at}
