"""Pure core: render a steward proposal into draft initiative and ticket text.

No file, clock or environment access. The caller passes `proposed_by` and `now`
(an ISO UTC string) and applies the returned mapping itself.

A proposal is a mapping with these keys:

- `id`: the proposal's own id; the draft id is derived from it.
- `title`: initiative title.
- `repo`: repository the work lands in.
- `files`: list of repo-relative file paths; they become each ticket's surfaces.
- `evidence`: non-empty list of strings backing the proposal (store rows, run ids, file and line).
- `tickets`: non-empty list of mappings with `phase`, `id`, `title`, `needs` (list of ticket
  ids in this proposal) and the prose paragraphs `current`, `change`, `tests`, `done_when`.

`ground` answers only the grounding question: a repo and at least one safe file path.
`draft_problem` answers whether the rest can be rendered. `render_draft` returns an empty
mapping when either says no, so a caller that writes what it is given writes nothing.

`steward.ceiling_candidates` rows carry role, model and ceiling numbers but no id, repo,
files or tickets. Building this shape from a candidate is the job of a separate adapter
module, and a raw candidate passed here grounds as `no repo`.

Two wrong beliefs this guards against. First, that `_yaml_scalar` quoting makes any value
safe in a header: it leaves a newline raw inside the quotes and `parse_frontmatter` splits
the header on newlines, so "x\\nstate: done" would write a second `state:` line that wins
when read. Every header value therefore goes through `_line`. Second, that a quoted list
item reads back as written: route's `_parse_list` splits on commas and never unquotes.
Lists that carry free text (`surfaces`, `evidence`) are written as block lists, one plain
item per line, which `parse_frontmatter` returns verbatim; an item that could not be plain
YAML is refused instead of quoted.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_tools.route import _yaml_scalar, slugify

# C0 and C1 control characters, plus the Unicode line and paragraph separators.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f" + chr(0x2028) + chr(0x2029) + "]+")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_PARAGRAPHS = ("current", "change", "tests", "done_when")
_NOT_PLAIN_LEAD = frozenset("[]{}#&*!|>'\"%@`,?")


@dataclass(frozen=True)
class Grounded:
    repo: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class Ungrounded:
    reason: str


def _line(value: str) -> str:
    """One header line: every run of control characters becomes a single space."""
    return _CONTROL.sub(" ", value).strip()


def _title(value: str) -> str:
    return _line(value.replace("`", ""))


def _plain(text: str) -> bool:
    """True when `text` is a block-list item YAML reads as the same plain string."""
    return (
        bool(text)
        and text[0] not in _NOT_PLAIN_LEAD
        and text != "-"
        and not text.startswith("- ")
        and not text.endswith(":")
        and ": " not in text
        and " #" not in text
    )


def _strings(value: Any) -> tuple[str, ...] | None:
    """A list or tuple of strings as a tuple; None for a bare string or a non-string entry."""
    return tuple(value) if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value) else None


def _safe_path(path: str) -> bool:
    return not (path.startswith("/") or ".." in path.split("/") or _CONTROL.search(path)) and _plain(path)


def ground(proposal: Mapping[str, Any]) -> Grounded | Ungrounded:
    """Grounded only when the proposal names a repo and at least one safe repo-relative file."""
    repo = proposal.get("repo")
    files = _strings(proposal.get("files", []))
    if not isinstance(repo, str) or not _line(repo):
        return Ungrounded("no repo")
    if files is None:
        return Ungrounded("files is not a list of strings")
    paths = tuple(f.strip() for f in files)
    if not any(paths):
        return Ungrounded("no file")
    if not all(paths):
        return Ungrounded("blank file entry")
    unsafe = [p for p in paths if not _safe_path(p)]
    return Ungrounded(f"unsafe file {unsafe[0]!r}") if unsafe else Grounded(_line(repo), paths)


def draft_id(proposal: Mapping[str, Any]) -> str:
    """Slug of the proposal's id plus eight hex of its sha256, so ids that slug alike stay distinct."""
    raw = str(proposal.get("id", ""))
    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"{slugify(raw) or 'draft'}-{digest}"


def _is_slug(value: Any) -> bool:
    return isinstance(value, str) and bool(_SLUG.match(value))


def _ticket_problem(ticket: Any, ids: frozenset[str]) -> str | None:
    if not isinstance(ticket, Mapping):
        return "ticket is not a mapping"
    if not (_is_slug(ticket.get("phase")) and _is_slug(ticket.get("id"))):
        return f"bad ticket path {ticket.get('phase')!r}/{ticket.get('id')!r}"
    if not isinstance(ticket.get("title"), str) or not _title(ticket["title"]):
        return f"no title for ticket {ticket['id']}"
    needs = _strings(ticket.get("needs", []))
    if needs is None:
        return f"needs of ticket {ticket['id']} is not a list of strings"
    if not set(needs) <= ids:
        return f"unknown need {sorted(set(needs) - ids)[0]!r} in ticket {ticket['id']}"
    if not all(isinstance(ticket.get(k, ""), str) for k in _PARAGRAPHS):
        return f"prose of ticket {ticket['id']} is not text"
    if not any(ticket.get(k, "").strip() for k in _PARAGRAPHS):
        return f"no prose for ticket {ticket['id']}"
    return None


def _stuck(needs: Mapping[str, frozenset[str]]) -> frozenset[str]:
    """Ticket ids that can never start: those left when every ticket with no open need is removed, repeatedly."""
    if not needs:
        return frozenset()
    ready = frozenset(k for k, v in needs.items() if not v)
    if not ready:
        return frozenset(needs)
    return _stuck({k: v - ready for k, v in needs.items() if k not in ready})


def _tickets_problem(tickets: Any) -> str | None:
    if not isinstance(tickets, (list, tuple)) or not tickets:
        return "no ticket"
    ids = frozenset(t["id"] for t in tickets if isinstance(t, Mapping) and isinstance(t.get("id"), str))
    problems = [p for p in (_ticket_problem(t, ids) for t in tickets) if p]
    if problems:
        return problems[0]
    names = [t["id"] for t in tickets]
    duplicates = [n for i, n in enumerate(names) if n in names[:i]]
    if duplicates:
        return f"duplicate ticket id {duplicates[0]}"
    stuck = _stuck({t["id"]: frozenset(t.get("needs", ())) for t in tickets})
    return f"needs cycle through {sorted(stuck)[0]}" if stuck else None


def _evidence_problem(evidence: Any) -> str | None:
    items = _strings(evidence)
    if items is None:
        return "evidence is not a list of strings"
    lined = [_line(e) for e in items]
    if not lined:
        return "no evidence"
    if not all(lined):
        return "blank evidence item"
    return None if all(_plain(e) for e in lined) else "evidence item is not plain text"


def _stamp_problem(proposed_by: Any, now: Any) -> str | None:
    if not isinstance(proposed_by, str) or not _line(proposed_by):
        return "no proposed_by"
    return None if isinstance(now, str) and _ISO_UTC.match(now) else "now is not ISO UTC"


def draft_problem(proposal: Mapping[str, Any], proposed_by: str, now: str) -> str | None:
    """One-line reason `render_draft` would return nothing for these inputs; None when it can render."""
    if not isinstance(proposal.get("id"), str) or not proposal["id"].strip():
        return "no id"
    if not isinstance(proposal.get("title"), str) or not _title(proposal["title"]):
        return "no title"
    return (
        _evidence_problem(proposal.get("evidence", []))
        or _tickets_problem(proposal.get("tickets", []))
        or _stamp_problem(proposed_by, now)
    )


def _scalar(value: str) -> str:
    return _yaml_scalar(_line(value))


def _flow(items: Sequence[str]) -> str:
    """A flow list of slugs; only ever given values `_is_slug` accepted, so nothing needs quoting."""
    return "[" + ", ".join(items) + "]"


def _entry(key: str, value: str | tuple[str, ...]) -> list[str]:
    """Header lines for one field; a tuple is a block list, or `[]` when empty."""
    if isinstance(value, str):
        return [f"{key}: {value}"]
    return [f"{key}:", *(f"  - {item}" for item in value)] if value else [f"{key}: []"]


def _document(fields: Sequence[tuple[str, str | tuple[str, ...]]], body: str) -> str:
    header = "\n".join(line for key, value in fields for line in _entry(key, value))
    return f"---\n{header}\n---\n\n{body}\n"


def _ticket_text(ticket: Mapping[str, Any], surfaces: tuple[str, ...]) -> str:
    fields = [
        ("id", ticket["id"]),
        ("phase", ticket["phase"]),
        ("state", "todo"),
        ("needs", _flow(ticket.get("needs", ()))),
        ("surfaces", surfaces),
        ("title", _scalar(_title(ticket["title"]))),
    ]
    prose = (ticket.get(k, "").strip() for k in _PARAGRAPHS)
    return _document(fields, "\n\n".join(p for p in prose if p))


def render_draft(
    proposal: Mapping[str, Any], grounded: Grounded | Ungrounded, proposed_by: str, now: str
) -> dict[str, str]:
    """`<draft-id>/initiative.md` and `<draft-id>/<phase>/<task-id>.md`, with no `work/` root.

    `now` is ISO UTC. Empty when `grounded` is Ungrounded or `draft_problem` names a reason.
    """
    if isinstance(grounded, Ungrounded) or draft_problem(proposal, proposed_by, now):
        return {}
    slug = draft_id(proposal)
    title = _title(proposal["title"])
    initiative = _document(
        [
            ("id", slug),
            ("title", _scalar(title)),
            ("repo", _scalar(grounded.repo)),
            ("draft", "true"),
            ("proposed_by", _scalar(proposed_by)),
            ("proposed_at", _scalar(now)),
            ("evidence", tuple(_line(e) for e in proposal["evidence"])),
        ],
        title,
    )
    tickets = {f"{slug}/{t['phase']}/{t['id']}.md": _ticket_text(t, grounded.files) for t in proposal["tickets"]}
    return {f"{slug}/initiative.md": initiative, **tickets}
