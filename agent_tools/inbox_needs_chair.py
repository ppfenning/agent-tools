"""Inbox source for open needs_chair rows in the chair action log."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from agent_tools.chair_read_record import ACTION_LOG
from agent_tools.chair_remedy import remedy_for
from agent_tools.dash_chair_action import _parsed  # the existing action-log line parser; reused, not copied
from agent_tools.inbox import InboxItem, item_id

# chair_remedy maps cause "stale" to drop and cause "ticket" to re-ground; the argv depends only on the remedy kind.
_DROP_CAUSE = "stale"
_REGROUND_CAUSE = "ticket"

# Neither existing "closed" rule fits these rows. dash_feed._needs_chair_open counts courier refs, not action-log rows.
# dash_chair_action closes an open line, one with no `status`, on a later line with a status. But chair_exec records
# every needs_chair with status "recorded", so under that rule every row is closed the moment it is written.
# A row closes instead on a later action that succeeded on its target: a landed land or land_phase, a relaunch that
# ran (`done`), or a check_login whose recorded row reports login_ok true for the row's host.
_CLOSING_STATUS = {"land": "landed", "land_phase": "landed", "relaunch": "done", "check_login": "recorded"}

_NO_TIME = datetime(1970, 1, 1, tzinfo=UTC)

Target = tuple[str, ...]


def _text(record: Mapping, key: str) -> str:
    return str(record.get(key) or "")


def _target(record: Mapping) -> Target:
    """The task id, else (initiative, phase), else (initiative,), else ("host", host); () when none is named."""
    task, initiative, phase, host = (_text(record, k) for k in ("task_id", "initiative", "phase", "host"))
    if task:
        return (task,)
    if initiative:
        return (initiative, phase) if phase else (initiative,)
    return ("host", host) if host else ()


def _row_keys(row: Mapping) -> frozenset[Target]:
    """The closer keys that close a needs_chair row: its own target, and its initiative for a phase row."""
    target = _target(row)
    initiative_wide = {(target[0],)} if len(target) == 2 and target[0] != "host" else set()
    return frozenset({target, *initiative_wide}) if target else frozenset()


def _login_ok(reason: str) -> bool:
    """True when a check_login's recorded row says login_ok true, at top level or under versions."""
    try:
        row = json.loads(reason)
    except ValueError:
        return False
    if not isinstance(row, dict):
        return False
    versions = row.get("versions")
    nested = isinstance(versions, dict) and versions.get("login_ok") is True
    return row.get("login_ok") is True or nested


def _closer_key(action: Mapping) -> Target | None:
    """The target a successful later action closes; None when it closes nothing."""
    kind = _text(action, "kind")
    if kind not in _CLOSING_STATUS or action.get("status") != _CLOSING_STATUS[kind]:
        return None
    if kind == "check_login":
        return ("host", _text(action, "host")) if action.get("host") and _login_ok(_text(action, "reason")) else None
    return _target(action) or None


def _created_at(record: Mapping) -> datetime:
    """The record's ts in UTC; a naive ts is taken as UTC and a missing or bad one is the epoch."""
    try:
        then = datetime.fromisoformat(_text(record, "ts"))
    except ValueError:
        return _NO_TIME
    return then.astimezone(UTC) if then.tzinfo else then.replace(tzinfo=UTC)


def _identity(record: Mapping) -> Target:
    """The de-duplication key: the target, or for a record naming none its cause and ts, so unnamed rows stay apart."""
    return _target(record) or ("?", _text(record, "cause"), _text(record, "ts"))


def _label(record: Mapping) -> str:
    target = _target(record)
    return f"host {target[1]}" if target[:1] == ("host",) else "/".join(target) or "?"


def _item(record: Mapping) -> InboxItem:
    # The chair pinned empty strings for the run, task and repo an initiative or host row does not carry.
    # The argv this gives names no ticket; the stored command, when present, is the one meant to be run.
    task = _text(record, "task_id")
    run = (_text(record, "run") or _text(record, "initiative")) if task else ""
    repo = _text(record, "repo") if task else ""
    command = record.get("command")
    accept = tuple(command) if command else tuple(remedy_for(_REGROUND_CAUSE, run, task, repo)["command"])
    url = _text(record, "url")
    return InboxItem(
        id=item_id("needs-chair", "/".join(_identity(record))),
        kind="needs-chair",
        created_at=_created_at(record),
        what=f"{_label(record)}: {_text(record, 'cause') or '?'}" + (f" {url}" if url else ""),
        evidence=_text(record, "reason") if command else "",
        accept_cmd=accept,
        deny_cmd=tuple(remedy_for(_DROP_CAUSE, run, task, repo)["command"]),
    )


def needs_chair_to_items(records: Sequence[Mapping]) -> tuple[InboxItem, ...]:
    """One needs-chair item per target; the latest needs_chair record for a target wins."""
    latest = {_identity(r): r for r in records if r.get("kind") == "needs_chair"}
    return tuple(_item(r) for r in latest.values())


def open_needs_chair(rows: Sequence[Mapping]) -> list[dict]:
    """The needs_chair rows that no later closing action follows, in log order; one pass from the end."""
    closed: set[Target] = set()
    kept: list[dict] = []
    # One backward pass keeps this linear in the log; the set and list are local to this call.
    for row in reversed(rows):
        if row.get("kind") == "needs_chair":
            if not (_row_keys(row) & closed):
                kept.append(dict(row))
        else:
            key = _closer_key(row)
            if key is not None:
                closed.add(key)
    return kept[::-1]


def load_needs_chair_records(runs_dir: Path) -> list[dict]:
    """Edge: open needs_chair rows from runs_dir / ACTION_LOG; [] when the log is missing."""
    try:
        lines = (runs_dir / ACTION_LOG).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return open_needs_chair([row for row in map(_parsed, lines) if row is not None])
