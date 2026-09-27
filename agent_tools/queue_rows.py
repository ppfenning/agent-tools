"""Pure codec between work item files and queue rows: text in, rows out, no I/O and no clock.

A row is a plain dict: kind, initiative, task_id, phase, state, needs, title, surfaces, body, extra.
`extra` holds every frontmatter key the named fields do not, so a parse then render loses nothing.
Frontmatter is read with PyYAML's BaseLoader, so every scalar stays the literal string the file wrote, as in
route.parse_frontmatter: `yes`, `1.10` and `2026-02-30` are never coerced to a bool, a float or a date.
"""

from __future__ import annotations

from typing import Any

import yaml

Row = dict[str, Any]

_TASK_DEFAULT_STATE = "todo"  # route.work_item's default for a file that names no state
_LIFTED = ("title", "needs", "surfaces")


def _split_frontmatter(text: str) -> tuple[dict, str] | None:
    lines = text.split("\n")
    closes = [i for i, line in enumerate(lines[1:], start=1) if line.rstrip() == "---"]
    if lines[0].rstrip() != "---" or not closes:
        return None
    try:
        fields = yaml.load("\n".join(lines[1 : closes[0]]), Loader=yaml.BaseLoader)
    except (yaml.YAMLError, ValueError):
        return None
    body = "\n".join(lines[closes[0] + 1 :])
    if fields is None:
        return {}, body
    return (fields, body) if isinstance(fields, dict) else None


def _strs(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _identity(kind: str, path_parts: tuple[str, ...]) -> tuple[str, str, str, str | None] | None:
    """(initiative, phase, task_id, state from the path or None). Tasks: (initiative, phase, file). Intake: (file,) or ("done", file)."""
    stem = path_parts[-1].removesuffix(".md") if path_parts else ""
    if kind == "task" and len(path_parts) == 3:
        return path_parts[0], path_parts[1], stem, None
    if kind == "intake" and len(path_parts) == 1:
        return "intake", "", stem, "queued"
    if kind == "intake" and len(path_parts) == 2 and path_parts[0] == "done":
        return "intake", "", stem, "done"
    return None


def parse_item(kind: str, path_parts: tuple[str, ...], text: str) -> Row | None:
    """None for an unknown kind or path shape, a file with no frontmatter, or frontmatter that will not parse."""
    identity, split = _identity(kind, path_parts), _split_frontmatter(text)
    if identity is None or split is None:
        return None
    fields, body = split
    initiative, phase, task_id, path_state = identity
    lifted = ("state", *_LIFTED) if path_state is None else _LIFTED
    return {
        "kind": kind,
        "initiative": initiative,
        "task_id": task_id,
        "phase": phase,
        "state": str(fields.get("state") or _TASK_DEFAULT_STATE) if path_state is None else path_state,
        "needs": _strs(fields.get("needs")),
        "title": str(fields.get("title") or ""),
        "surfaces": _strs(fields.get("surfaces")),
        "body": body,
        "extra": {k: v for k, v in fields.items() if k not in lifted},
    }


def render_item(row: Row) -> str:
    """Frontmatter keys in a fixed order: state (tasks only, an intake's state is its path), title, needs, surfaces, then extra sorted."""
    named = {
        **({"state": row["state"]} if row["kind"] == "task" else {}),
        "title": row["title"],
        "needs": row["needs"],
        "surfaces": row["surfaces"],
    }
    kept = {k: v for k, v in named.items() if k == "state" or v}
    extra = {k: row["extra"][k] for k in sorted(row["extra"], key=str) if k not in kept}
    fields = {**kept, **extra}
    header = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True, width=10**6) if fields else ""
    return f"---\n{header}---\n{row['body']}"


def row_path(row: Row) -> str:
    if row["kind"] == "task":
        return f"work/{row['initiative']}/{row['phase']}/{row['task_id']}.md"
    return f"intake/done/{row['task_id']}.md" if row["state"] == "done" else f"intake/{row['task_id']}.md"
