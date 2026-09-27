"""Fold a fetched run's approvals into local ticket state.

`chair_exec`'s `fetch_exit` action runs `cox runs fetch` for a remote run, then hands the run
and its initiative here. The run's task records are already on disk under `runs/<run>/tasks/`;
this reads them, decides which are approved by the same rule `land.py` applies before a land,
and moves each approved task's ticket file from `ready` to `approved`. Writing to the SQL work
store is out of bounds here: the ticket file's `state:` line is the record this task writes.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from agent_tools.land import _approved

__all__ = ["apply_fetched_approvals", "apply_ready_to_approved", "approved_task_ids"]


def approved_task_ids(records: Iterable[Mapping[str, Any]]) -> list[str]:
    """The `task` id of every record whose review and arbitration verdicts approve it (`land._approved` is None)."""
    return [str(record["task"]) for record in records if record.get("task") and _approved(dict(record)) is None]


def apply_ready_to_approved(text: str) -> tuple[str | None, str | None]:
    """The ticket's `state: ready` line rewritten to `state: approved`, paired with `None`; or `None` paired
    with `None` when the state is already `approved` or `done` (nothing to do); or `None` paired with a
    one-line refusal naming the state when it is anything else. Mirrors `land.approve_to_done`'s header scan."""
    if not text.startswith("---\n"):
        return None, "chair_apply_fetch: ticket has no state field"
    close = text.find("\n---\n", 4)
    header = text[:close] if close != -1 else text
    for line in header.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped.startswith("state:"):
            continue
        state = stripped[len("state:"):].strip()
        if state in ("approved", "done"):
            return None, None
        if state != "ready":
            return None, f"chair_apply_fetch: ticket state is {state!r}, not moving to approved"
        return text.replace(line, line.replace("ready", "approved", 1), 1), None
    return None, "chair_apply_fetch: ticket has no state field"


def _task_records(runs_dir: Path, run: str) -> list[dict[str, Any]]:
    """Edge. Every parseable dict under `runs/<run>/tasks/<phase>/<task>.json`; a bad file is skipped, not raised."""
    records = []
    for path in sorted((runs_dir / run / "tasks").glob("*/*.json")):
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _ticket_path(work_dir: Path, initiative: str, task_id: str) -> Path | None:
    """Edge. The one ticket file under `work/<initiative>/**/<task_id>.md`; None when none matches."""
    return next(iter(sorted((work_dir / "work" / initiative).glob(f"**/{task_id}.md"))), None)


def apply_fetched_approvals(runs_dir: Path, work_dir: Path, run: str, initiative: str) -> list[str]:
    """Edge. Move each approved task's ticket from ready to approved; the ids actually changed, in order.

    A missing or malformed task record, or a ticket file that is not found, is skipped and never raises.
    """
    changed = []
    for task_id in approved_task_ids(_task_records(runs_dir, run)):
        path = _ticket_path(work_dir, initiative, task_id)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text, _ = apply_ready_to_approved(text)
        if new_text is None:
            continue
        path.write_text(new_text, encoding="utf-8")
        changed.append(task_id)
    return changed
