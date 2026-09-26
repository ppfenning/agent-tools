"""Edge for `cox steward draft`: proposals become draft initiatives under `work/`.

Proposals are read from `<workspace>/intake`, where `cox steward propose` leaves them.
Two file shapes are read:

- `*.md`: the frontmatter `intake_file` writes (`id`, `title`, `repo`), plus `files` and
  `evidence` when present as lists, and the body as `body`. A ceiling proposal from
  `steward propose` carries no `files`, `evidence` or `tickets`, so it is listed as
  ungrounded, never written.
- `*.json`: one proposal mapping in the shape `draft_render` documents, tickets included.

Nothing is approved and no store is written. Every ticket renders `state: todo` and
initiative.md renders `draft: true`; the drafts reach the store through route sync.
A draft directory that already exists is never opened or rewritten.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import draft_render, route

PROPOSED_BY = "steward"


@dataclass(frozen=True)
class Verdict:
    kind: str  # "drafted", "exists" or "ungrounded"
    id: str
    reason: str = ""
    files: Mapping[str, str] = field(default_factory=dict)


def _label(proposal: Mapping[str, Any]) -> str:
    return str(proposal.get("id") or "(no id)")


def _verdict(proposal: Mapping[str, Any], existing: frozenset[str], proposed_by: str, now: str) -> Verdict:
    grounded = draft_render.ground(proposal)
    if isinstance(grounded, draft_render.Ungrounded):
        return Verdict("ungrounded", _label(proposal), grounded.reason)
    slug = draft_render.draft_id(proposal)
    if slug in existing:
        return Verdict("exists", slug)
    problem = draft_render.draft_problem(proposal, proposed_by, now)
    if problem:
        return Verdict("ungrounded", _label(proposal), problem)
    return Verdict("drafted", slug, files=draft_render.render_draft(proposal, grounded, proposed_by, now))


def _settle(verdicts: list[Verdict]) -> list[Verdict]:
    """A second proposal that would draft an id an earlier one drafts is `exists`, never a rewrite."""
    return [
        Verdict("exists", v.id)
        if v.kind == "drafted" and any(u.kind == "drafted" and u.id == v.id for u in verdicts[:i])
        else v
        for i, v in enumerate(verdicts)
    ]


def plan_drafts(
    proposals: list[Mapping[str, Any]], existing: frozenset[str], proposed_by: str, now: str
) -> dict[str, Any]:
    """Pure. `files` maps `<draft-id>/...` to text; `drafted` and `exists` list draft ids; `ungrounded` lists id and reason."""
    settled = _settle([_verdict(p, existing, proposed_by, now) for p in proposals])
    return {
        "files": {rel: text for v in settled if v.kind == "drafted" for rel, text in v.files.items()},
        "drafted": [v.id for v in settled if v.kind == "drafted"],
        "exists": [v.id for v in settled if v.kind == "exists"],
        "ungrounded": [{"id": v.id, "reason": v.reason} for v in settled if v.kind == "ungrounded"],
    }


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_markdown(path: Path) -> dict[str, Any]:
    fields, body = route.parse_frontmatter(path.read_text(encoding="utf-8"))
    return {**fields, "id": fields.get("id") or path.stem, "body": body}


def _from_json(path: Path) -> dict[str, Any] | str:
    """The proposal mapping, or the reason the file is not one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"unreadable proposal: {exc}"
    return data if isinstance(data, dict) else "proposal is not a JSON object"


def read_proposals(intake: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Proposals in file-name order, and the JSON files that could not be read as one."""
    markdown = [_from_markdown(p) for p in sorted(intake.glob("*.md"))]
    loaded = [(p, _from_json(p)) for p in sorted(intake.glob("*.json"))]
    proposals = [{**data, "id": data.get("id") or p.stem} for p, data in loaded if isinstance(data, dict)]
    unreadable = [{"id": p.stem, "reason": data} for p, data in loaded if isinstance(data, str)]
    return [*markdown, *proposals], unreadable


def _existing(work: Path) -> frozenset[str]:
    return frozenset(d.name for d in work.iterdir() if d.is_dir()) if work.is_dir() else frozenset()


def _render(plan: Mapping[str, Any]) -> str:
    lines = [
        line
        for name, items in (("drafted", plan["drafted"]), ("exists", plan["exists"]))
        for line in (f"{name} ({len(items)}):", *(f"  {i}" for i in items))
    ]
    bad = plan["ungrounded"]
    return "\n".join([*lines, f"ungrounded ({len(bad)}):", *(f"  {b['id']}: {b['reason']}" for b in bad)])


def run_draft(workspace: Path, *, as_json: bool) -> int:
    """Read intake, write each new grounded draft under `<workspace>/work`, print the three lists."""
    proposals, unreadable = read_proposals(workspace / "intake")
    work = workspace / "work"
    plan = plan_drafts(proposals, _existing(work), PROPOSED_BY, now_utc())
    for rel, text in plan["files"].items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text, encoding="utf-8")
    report = {
        "drafted": plan["drafted"],
        "exists": plan["exists"],
        "ungrounded": [*plan["ungrounded"], *unreadable],
    }
    print(json.dumps(report) if as_json else _render(report))
    return 0
