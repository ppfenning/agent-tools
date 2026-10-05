"""Pure inbox core: item model, ordering, rendering and verb runner."""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Literal

Kind = Literal["draft", "approval", "needs-chair", "pr", "refused-command"]
Verb = Literal["accept", "deny"]


@dataclass(frozen=True)
class InboxItem:
    id: str
    kind: Kind
    created_at: datetime  # UTC, passed in
    what: str
    evidence: str
    accept_cmd: tuple[str, ...]
    deny_cmd: tuple[str, ...]


@dataclass(frozen=True)
class Found:
    item: InboxItem


@dataclass(frozen=True)
class NotFound:
    query: str


@dataclass(frozen=True)
class Ambiguous:
    query: str
    ids: tuple[str, ...]


def item_id(kind: str, source_key: str) -> str:
    """Eight hex chars of sha256 over kind, NUL and source_key; stable across runs."""
    return hashlib.sha256(f"{kind}\0{source_key}".encode()).hexdigest()[:8]


def sort_oldest_first(items: Sequence[InboxItem]) -> tuple[InboxItem, ...]:
    return tuple(sorted(items, key=lambda i: (i.created_at, i.id)))


def render_text(items: Sequence[InboxItem], tz: tzinfo) -> str:
    if not items:
        return "Inbox is empty."
    return "\n".join(
        f"{i.id}  {i.kind}  {i.created_at.astimezone(tz):%Y-%m-%d %H:%M}  {i.what}\n    {i.evidence}" for i in items
    )


def render_json(items: Sequence[InboxItem]) -> list[dict]:
    return [
        {
            "id": i.id,
            "kind": i.kind,
            "created_at": i.created_at.isoformat(),
            "what": i.what,
            "evidence": i.evidence,
            "accept_cmd": list(i.accept_cmd),
            "deny_cmd": list(i.deny_cmd),
        }
        for i in items
    ]


def find_item(items: Sequence[InboxItem], id: str) -> Found | NotFound | Ambiguous:
    """An exact id wins; otherwise a unique id prefix matches."""
    exact = [i for i in items if i.id == id]
    if exact:
        return Found(exact[0])
    prefixed = [i for i in items if id and i.id.startswith(id)]
    if len(prefixed) == 1:
        return Found(prefixed[0])
    if prefixed:
        return Ambiguous(id, tuple(i.id for i in prefixed))
    return NotFound(id)


def run_verb(item: InboxItem, verb: Verb, run: Callable[[Sequence[str]], int]) -> int:
    """Print the exact command line, then run it and return its exit code."""
    if verb == "accept":
        argv = item.accept_cmd
    elif verb == "deny":
        argv = item.deny_cmd
    else:
        raise ValueError(f"unknown verb: {verb!r}")
    print(shlex.join(argv))
    return run(argv)
