"""Pure codec between work item files and queue rows: text in, rows out, no I/O and no clock.

A row is a plain dict: kind, initiative, task_id, phase, state, needs, title, surfaces, body, extra.
`extra` holds every frontmatter key the named fields do not, so a parse then render loses nothing.
Frontmatter is read with PyYAML's BaseLoader, so every scalar stays the literal string the file wrote, as in
route.parse_frontmatter: `yes`, `1.10` and `2026-02-30` are never coerced to a bool, a float or a date.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

Row = dict[str, Any]

_TASK_DEFAULT_STATE = "todo"  # route.work_item's default for a file that names no state
_LIFTED = ("title", "needs", "surfaces")
_INTAKE_STAMPED_STATE = "done"  # cli._route_file_from_intake writes the stamped intake to intake/done/
_INITIATIVE_LINE_STATES = ("landed", "decomposed")  # intake states that export to intake/ and carry an initiative
# initiative.md names no state: route.initiative_states derives done from its tasks. The row holds the default.
_INITIATIVE_STATE = _TASK_DEFAULT_STATE


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


def requires_of(fields: Any) -> list[str]:
    """A work item's `requires:` capabilities, read the way `needs` is; [] when it names none."""
    return _strs(fields.get("requires")) if isinstance(fields, Mapping) else []


def row_requires(row: Row) -> list[str]:
    """`requires:` from a parsed or stored row. It stays in `extra`, not a lifted field: the store's work_items
    table has no requires column, so a lifted key would never persist and every import would read as changed."""
    return requires_of(row.get("extra"))


def _identity(kind: str, path_parts: tuple[str, ...]) -> tuple[str, str, str, str | None] | None:
    """(initiative, phase, task_id, state from the path or None). Tasks: (initiative, phase, file). Intake: (file,) or ("done", file).
    Initiative: (id, "initiative.md"), keyed by its id."""
    stem = path_parts[-1].removesuffix(".md") if path_parts else ""
    if kind == "task" and len(path_parts) == 3:
        return path_parts[0], path_parts[1], stem, None
    if kind == "initiative" and len(path_parts) == 2 and path_parts[1] == "initiative.md":
        return path_parts[0], "", path_parts[0], _INITIATIVE_STATE
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


def _initiative_line(row: Row) -> str | None:
    """extra.initiative, else the row's own id when it names an initiative: the row's `initiative` field equals it."""
    if row["kind"] != "intake" or row["state"] not in _INITIATIVE_LINE_STATES:
        return None
    own_id = str(row["extra"].get("id") or row["task_id"])
    return row["extra"].get("initiative") or (own_id if row["initiative"] == own_id else None)


def render_item(row: Row) -> str:
    """Frontmatter keys in a fixed order: state (tasks only, an intake's state is its path), title, needs, surfaces, then extra sorted.
    A landed or decomposed intake also gets `initiative:`, so route.intake_groups reads it as decomposed."""
    named = {
        **({"state": row["state"]} if row["kind"] == "task" else {}),
        "title": row["title"],
        "needs": row["needs"],
        "surfaces": row["surfaces"],
    }
    kept = {k: v for k, v in named.items() if k == "state" or v}
    initiative = _initiative_line(row)
    with_line = {**row["extra"], **({"initiative": initiative} if initiative else {})}
    extra = {k: with_line[k] for k in sorted(with_line, key=str) if k not in kept}
    fields = {**kept, **extra}
    header = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True, width=10**6) if fields else ""
    return f"---\n{header}---\n{row['body']}"


def _task_state(needs: list[str]) -> str:
    """Decompose's rule: `ready` with no needs, `todo` otherwise."""
    return _TASK_DEFAULT_STATE if needs else "ready"


def plan_to_rows(plan: Mapping[str, Any], intake_id: str) -> list[Row]:
    """The rows parse_item gives for the plan's initiative.md, then its task files, in plan order."""
    # A ticket's needs_json and surfaces_json are the `needs` and `surfaces` lists below: this row shape has no _json keys.
    initiative = plan["id"]
    head: Row = {
        "kind": "initiative",
        "initiative": initiative,
        "task_id": initiative,
        "phase": "",
        "state": _INITIATIVE_STATE,
        "needs": [],
        "title": str(plan["title"]),
        "surfaces": [],
        "body": plan["body"],
        "extra": {
            "id": initiative,
            "intake": f"intake/{intake_id}.md",  # route.link_intake's form: the intake path, not its id
            "phases": [{"id": p["id"], "goal": p["goal"]} for p in plan["phases"]],
        },
    }
    tasks = [
        {
            "kind": "task",
            "initiative": initiative,
            "task_id": t["id"],
            "phase": t["phase"],
            "state": _task_state(_strs(t.get("needs"))),
            "needs": _strs(t.get("needs")),
            "title": str(t["title"]),
            "surfaces": _strs(t.get("surfaces")),
            "body": t["body"],
            "extra": {"id": t["id"], "phase": t["phase"]},  # frontmatter keys decompose writes, which parse_item leaves in extra
        }
        for t in plan["tasks"]
    ]
    return [head, *tasks]


def intake_stamp(intake_row: Row, initiative_id: str) -> Row:
    """The stamped file's row: `initiative:` goes in `extra` as parse_item reads it; top-level `initiative` stays the key `intake`."""
    extra = {**intake_row["extra"], "initiative": initiative_id}
    return {**intake_row, "state": _INTAKE_STAMPED_STATE, "extra": extra}


def row_path(row: Row) -> str:
    if row["kind"] == "task":
        return f"work/{row['initiative']}/{row['phase']}/{row['task_id']}.md"
    if row["kind"] == "initiative":
        return f"work/{row['initiative']}/initiative.md"
    return f"intake/done/{row['task_id']}.md" if row["state"] == "done" else f"intake/{row['task_id']}.md"
