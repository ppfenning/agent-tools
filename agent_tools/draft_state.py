"""Approve and decline a draft initiative as pure text rewrites; the edge reads, writes and mirrors."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

TODO, READY, DROPPED = "todo", "ready", "dropped"
_KEY = re.compile(r"^([A-Za-z_][\w-]*):")
_PLAIN = re.compile(r"^[\w.@/+-]+$")


@dataclass(frozen=True)
class Plan:
    initiative_text: str
    tickets: Mapping[str, str]  # rewritten tickets only; untouched ones are absent
    moves: tuple[tuple[str, str, str], ...]  # (task_id, from_state, to_state)


@dataclass(frozen=True)
class Refusal:
    reason: str


def _eol(line: str) -> str:
    return line[len(line.rstrip("\r\n")) :]


def _key(line: str) -> str | None:
    m = _KEY.match(line)
    return m.group(1) if m else None


def _split(text: str) -> tuple[str, list[str], str] | None:
    """Return (opening fence, frontmatter lines, closing fence and body), or None without frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    closes = [i for i, line in enumerate(lines) if i > 0 and line.rstrip("\r\n") == "---"]
    return (lines[0], lines[1 : closes[0]], "".join(lines[closes[0] :])) if closes else None


def read_field(lines: list[str], key: str) -> str | None:
    values = [line.split(":", 1)[1].strip().strip("\"'") for line in lines if _key(line) == key]
    return values[0] if values else None


def set_fields(lines: list[str], updates: Mapping[str, str]) -> list[str]:
    """Replace matching key lines in place; append absent keys at the end. Other lines are kept as is."""
    keys = [_key(line) for line in lines]
    replaced = [f"{k}: {updates[k]}{_eol(line)}" if k in updates else line for k, line in zip(keys, lines)]
    return [*replaced, *(f"{k}: {v}\n" for k, v in updates.items() if k not in keys)]


def _rewrite(text: str, updates: Mapping[str, str]) -> str:
    parts = _split(text)
    return text if parts is None else parts[0] + "".join(set_fields(parts[1], updates)) + parts[2]


def _field(text: str, key: str) -> str | None:
    parts = _split(text)
    return None if parts is None else read_field(parts[1], key)


def _is_draft(initiative_text: str) -> bool:
    return (_field(initiative_text, "draft") or "").lower() == "true"


def _scalar(value: str) -> str:
    return value if _PLAIN.match(value) else json.dumps(value, ensure_ascii=False)


def _todo(tickets: Mapping[str, str]) -> list[str]:
    return [task_id for task_id, text in tickets.items() if _field(text, "state") == TODO]


def _plan(
    initiative_text: str, tickets: Mapping[str, str], moved: list[str], to_state: str, updates: Mapping[str, str]
) -> Plan:
    return Plan(
        initiative_text=_rewrite(initiative_text, updates),
        tickets={task_id: _rewrite(tickets[task_id], {"state": to_state}) for task_id in moved},
        moves=tuple((task_id, TODO, to_state) for task_id in moved),
    )


def plan_approve(
    initiative_text: str, tickets: Mapping[str, str], task_id: str | None, by: str, now: datetime
) -> Plan | Refusal:
    todo = _todo(tickets)
    moved = todo if task_id is None else [t for t in todo if t == task_id]
    if not _is_draft(initiative_text):
        return Refusal("initiative is not a draft")
    if task_id is not None and task_id not in tickets:
        return Refusal(f"no ticket named {task_id}")
    if not moved:
        return Refusal("no todo ticket to approve")
    finished = len(moved) == len(todo)
    updates = {**({"draft": "false"} if finished else {}), "approved_by": _scalar(by), "approved_at": now.isoformat()}
    return _plan(initiative_text, tickets, moved, READY, updates)


def plan_decline(
    initiative_text: str, tickets: Mapping[str, str], reason: str, by: str, now: datetime
) -> Plan | Refusal:
    moved = _todo(tickets)
    if not reason.strip():
        return Refusal("a decline needs a reason")
    if not _is_draft(initiative_text):
        return Refusal("initiative is not a draft")
    if not moved:
        return Refusal("no todo ticket to decline")
    updates = {
        "draft": "false",
        "declined_by": _scalar(by),
        "declined_at": now.isoformat(),
        "declined_reason": json.dumps(reason.strip(), ensure_ascii=False),
    }
    return _plan(initiative_text, tickets, moved, DROPPED, updates)
