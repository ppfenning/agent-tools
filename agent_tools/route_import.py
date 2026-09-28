"""Pure plan for loading work item files into the store's `work_items` table.

Walking the workspace and driving `run_store.upsert_row` are the edge's job
(`agent_tools.cli._route_import`); this module only turns already-read
`(path_parts, text)` pairs into rows with `queue_rows.parse_item` and decides,
against the rows `read_queue` returned, which ones changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_tools import intake_state, queue_rows

Row = queue_rows.Row

_KINDS = {"work": "task", "intake": "intake"}
_TERMINAL = frozenset({"done", "dropped"})


@dataclass(frozen=True)
class Plan:
    """`to_write`: rows absent from or different than the store. `unchanged`/`skipped` are counts only,
    so a caller never has to hold the rows it will not write."""

    to_write: tuple[Row, ...]
    unchanged: int
    skipped: int


def _kind_and_rest(path_parts: tuple[str, ...]) -> tuple[str, tuple[str, ...]] | None:
    """(`kind`, the path under it) for a path rooted at `work/` or `intake/`, else None."""
    if not path_parts:
        return None
    kind = _KINDS.get(path_parts[0])
    return None if kind is None else (kind, tuple(path_parts[1:]))


def _parsed(path_parts: tuple[str, ...], text: str) -> Row | None:
    """The row for one file, or None when it is not an item: no `work/`/`intake/` root, `initiative.md`,
    or frontmatter `queue_rows.parse_item` refuses."""
    if not path_parts or path_parts[-1] == "initiative.md":
        return None
    classified = _kind_and_rest(path_parts)
    return None if classified is None else queue_rows.parse_item(*classified, text)


def _key(row: Row) -> tuple[str, str]:
    """The store's upsert key. It is not the file path: `intake/x.md` and `intake/done/x.md` share it."""
    return row["initiative"], row["task_id"]


def _unchanged(row: Row, stored: Row) -> bool:
    """True when every field `row` names already reads the same way in `stored`; extra store-only
    columns (a claim's holder, epoch, expiry) never count against a match."""
    return all(stored.get(field) == value for field, value in row.items())


def _initiative_entry(path_parts: tuple[str, ...], text: str) -> tuple[str, str] | None:
    """(initiative id, text) for a `work/<id>/initiative.md` path, else None."""
    is_initiative = len(path_parts) == 3 and path_parts[0] == "work" and path_parts[2] == "initiative.md"
    return (path_parts[1], text) if is_initiative else None


def _initiative_done(initiative_id: str, rows: list[Row]) -> bool:
    """True when every task row naming `initiative_id` is `done` or `dropped`; vacuously True for an id
    with no task rows, matching route.initiative_states."""
    return all(
        row["state"] in _TERMINAL for row in rows if row["kind"] == "task" and row["initiative"] == initiative_id
    )


def _intake_entry(row: Row) -> dict:
    """`row` in the entry shape `intake_state.intake_group` takes. `path` is never `done/`-prefixed: a
    queued or a done row names the same path, matching `cli._intake_entry_from_row`."""
    return {"path": f"intake/{row['task_id']}.md", "initiative": row["extra"].get("initiative"), "done": row["state"] == "done"}


def _resolved(row: Row, initiatives_by_id: dict, initiatives: list) -> Row:
    """`row` with an intake-kind's hardcoded path state replaced by `intake_state.intake_group`'s rule;
    a task row is returned unchanged."""
    if row["kind"] != "intake":
        return row
    return {**row, "state": intake_state.intake_group(_intake_entry(row), initiatives_by_id, initiatives)}


def plan_import(files: list[tuple[tuple[str, ...], str]], existing_rows: list[Row]) -> Plan:
    """The rows to write, plus how many already matched the store (`unchanged`) or were not imported
    (`skipped`: unparseable, `initiative.md`, or a file whose store key an earlier file in `files` holds).
    First file per key wins, so two files sharing a key cannot overwrite each other on every run.

    An intake-kind row's `state` is not the hardcoded path state: it is `intake_state.intake_group`'s
    verdict, given the `id`/`done`/`text` initiatives list route status builds from the `initiative.md`
    files already present in `files` — that file still writes no row of its own."""
    outcomes = [_parsed(path_parts, text) for path_parts, text in files]
    parsed = [row for row in outcomes if row is not None]
    initiative_texts = dict(filter(None, (_initiative_entry(path_parts, text) for path_parts, text in files)))
    initiatives = [{"id": iid, "done": _initiative_done(iid, parsed), "text": text} for iid, text in initiative_texts.items()]
    initiatives_by_id = {i["id"]: i for i in initiatives}
    resolved = [_resolved(row, initiatives_by_id, initiatives) for row in parsed]
    first_at = {_key(row): i for i, row in reversed(list(enumerate(resolved)))}
    kept = [row for i, row in enumerate(resolved) if first_at[_key(row)] == i]
    stored_by_key = {_key(row): row for row in existing_rows}
    to_write = [row for row in kept if not _unchanged(row, stored_by_key.get(_key(row), {}))]
    return Plan(to_write=tuple(to_write), unchanged=len(kept) - len(to_write), skipped=len(outcomes) - len(kept))


def format_summary(written: int, plan: Plan) -> str:
    return f"written {written}, unchanged {plan.unchanged}, skipped {plan.skipped}"
