"""Apply a `widen_ticket` action to its ticket file; `chair_exec._widen_ticket` mirrors the `ready` state to the store."""
from __future__ import annotations

from pathlib import Path

from agent_tools.chair_types import Action
from agent_tools.chair_widen_ticket import widen_ticket_text

__all__ = ["apply_widen"]


def _ticket_path(work_dir: Path, initiative: str, task_id: str) -> Path | None:
    """Edge. The one ticket file under `work/<initiative>/**/<task_id>.md`; None when none matches."""
    return next(iter(sorted((work_dir / "work" / initiative).glob(f"**/{task_id}.md"))), None)


def apply_widen(work_dir: Path, action: Action, when: str) -> tuple[bool, str]:
    """Edge. Never raises. `(True, reason)` naming the task and paths once the file is written; else `(False, note)`."""
    task_id, initiative = action.get("task_id", ""), action.get("initiative", "")
    paths = list(action.get("paths", []))
    path = _ticket_path(work_dir, initiative, task_id) if task_id and initiative else None
    if path is None:
        return False, "ticket not found"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"widen: cannot read {task_id}: {exc}"
    new_text, refusal = widen_ticket_text(text, list(zip(paths, action.get("additions", []))), when)
    if new_text is None:
        return False, refusal or "widen: rewrite refused"
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return False, f"widen: cannot write {task_id}: {exc}"
    return True, f"widened {task_id}: {', '.join(paths)}"
