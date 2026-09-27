"""Turn an initiative and its stale tasks into a draft as a pure text rewrite; the edge reads, writes and mirrors."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

TODO = "todo"
_KEY = re.compile(r"^([A-Za-z_][\w-]*):")
_PLAIN = re.compile(r"^[\w.@/+-]+$")


@dataclass(frozen=True)
class Plan:
    initiative_text: str
    tickets: Mapping[str, str]  # every ticket in ticket_texts; stale ones rewritten, the rest unchanged


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


def _sequence(values: Sequence[str]) -> str:
    return "[" + ", ".join(_scalar(v) for v in values) + "]"


def plan_stale_draft(
    initiative_text: str,
    ticket_texts: Mapping[str, str],
    stale_task_ids: Sequence[str],
    reason: str,
    since: str,
    now: str,
) -> Plan | Refusal:
    if _is_draft(initiative_text):
        return Refusal("initiative is already a draft")
    if not stale_task_ids:
        return Refusal("no stale task to draft")
    missing = [task_id for task_id in stale_task_ids if task_id not in ticket_texts]
    if missing:
        return Refusal(f"no ticket named {missing[0]}")
    stale = set(stale_task_ids)
    updates = {
        "draft": "true",
        "proposer": _scalar(f"chair (stale since {since}: {reason})"),
        "stale_tasks": _sequence(stale_task_ids),
    }
    tickets = {
        task_id: _rewrite(text, {"state": TODO}) if task_id in stale else text
        for task_id, text in ticket_texts.items()
    }
    return Plan(initiative_text=_rewrite(initiative_text, updates), tickets=tickets)
