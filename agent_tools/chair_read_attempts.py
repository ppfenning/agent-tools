"""Every attempt over all runs, oldest first, read from work items' frontmatter `attempts` lists."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from agent_tools import run_store
from agent_tools.chair_facts import RESCUE_FAILED_CAUSE, Key, run_initiative
from agent_tools.chair_read_quarantined import attempts_on_current_body, item_body
from agent_tools.stats_chair import frontmatter_item


def path_parts(path: str) -> tuple[str | None, str | None, str | None]:
    """(initiative, phase, task) from `.../work/<initiative>/<phase>/<task>.md`; None for a part the path lacks."""
    parts = Path(path).with_suffix("").parts
    at = max((i for i, part in enumerate(parts) if part == "work"), default=None)
    tail = parts[at + 1 :] if at is not None else ()
    return (tail[0], tail[1], tail[2]) if len(tail) == 3 else (None, None, None)


def attempt_rows(items: Iterable[tuple[str, Iterable[Mapping] | None]]) -> list[dict]:
    """One row per attempt, sorted by `ts` oldest first; a row without `ts` sorts first. The attempt's own fields win over the path's."""
    rows = [
        {
            **attempt,
            "path": path,
            "task": path_parts(path)[2],
            "initiative": attempt.get("initiative") or path_parts(path)[0],
            "phase": attempt.get("phase") or path_parts(path)[1],
            "run": attempt.get("run"),
            "cause": attempt.get("cause"),
        }
        for path, attempts in items
        for attempt in (attempts or [])
        if isinstance(attempt, Mapping)
    ]
    return sorted(rows, key=lambda row: str(row.get("ts") or ""))


def fill_causes(rows: Iterable[Mapping], store_rows: Iterable[Mapping]) -> list[dict]:
    """Each row with no `cause` takes the cause on the store's newest-seq attempt for its run and task; a file cause is kept."""
    newest = {(s["run_id"], s["task_id"]): s["cause"] for s in sorted(store_rows, key=lambda s: s["seq"])}
    return [{**row, "cause": row.get("cause") or newest.get((row.get("run"), row.get("task")))} for row in rows]


def current_body_starts(items: Iterable[tuple[str, str, Iterable[Mapping] | None]]) -> dict[Key, str]:
    """(initiative, phase, task) to the `ts` of the task's first attempt on its current body, from (path, body, attempts) items.

    An attempt with another `body_sha` was made on an earlier body. A task with no timed attempt on the current body has no key."""
    starts = {}
    for path, body, attempts in items:
        initiative, phase, task = path_parts(path)
        on_body = attempts_on_current_body([a for a in attempts or [] if isinstance(a, Mapping)], body)
        stamps = [str(a["ts"]) for a in on_body if a.get("ts")]
        if stamps and task is not None:
            starts[(str(initiative), str(phase), task)] = min(stamps)
    return starts


def with_stored_rescues(rows: Iterable[Mapping], store_rows: Iterable[Mapping], starts: Mapping[Key, str]) -> list[dict]:
    """The rows plus one rescue_failed row per store row at or after its task's first attempt on the current body, sorted by `ts`.

    A store row from before that attempt belongs to an earlier body, so a re-grounded ticket gets a fresh rescue."""
    rescues = [
        {
            "run": s["run_id"],
            "task": s["task_id"],
            "phase": s["phase_id"],
            "initiative": run_initiative(str(s["run_id"])),
            "ts": s["ts"],
            "kind": "rescue_failed",
            "cause": RESCUE_FAILED_CAUSE,
        }
        for s in store_rows
    ]
    kept = [r for r in rescues if _rescue_key(r) in starts and str(r["ts"] or "") >= starts[_rescue_key(r)]]
    return sorted([*rows, *kept], key=lambda row: str(row.get("ts") or ""))


def _rescue_key(row: Mapping) -> Key:
    return str(row["initiative"]), str(row["phase"]), str(row["task"])


def read_attempts(root: Path) -> list[dict]:
    """The edge: load each `work/<initiative>/<phase>/<task>.md` under `root` and hand its attempts to the core.

    The store's rescue_failed rows are added, since a failed rescue leaves no attempt in any file."""
    texts = {path: path.read_text(encoding="utf-8") for path in sorted((root / "work").glob("*/*/*.md"))}
    pairs = [(str(p.relative_to(root)), frontmatter_item(t, p.stem).get("attempts")) for p, t in texts.items()]
    starts = current_body_starts(
        (rel, item_body(text), attempts) for (rel, attempts), text in zip(pairs, texts.values(), strict=True)
    )
    rows = attempt_rows(pairs)
    runs = sorted({str(r["run"]) for r in rows if r["run"] and not r["cause"]})
    filled = fill_causes(rows, run_store.attempt_causes_for(root / "runs", runs))
    return with_stored_rescues(filled, run_store.rescue_failures(root / "runs"), starts)
