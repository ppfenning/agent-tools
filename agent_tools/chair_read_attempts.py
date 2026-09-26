"""Every attempt over all runs, oldest first, read from work items' frontmatter `attempts` lists."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from agent_tools import run_store
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


def read_attempts(root: Path) -> list[dict]:
    """The edge: load each `work/<initiative>/<phase>/<task>.md` under `root` and hand its attempts to the core."""
    pairs = [
        (
            str(path.relative_to(root)),
            frontmatter_item(path.read_text(encoding="utf-8"), path.stem).get("attempts"),
        )
        for path in sorted((root / "work").glob("*/*/*.md"))
    ]
    rows = attempt_rows(pairs)
    runs = sorted({str(r["run"]) for r in rows if r["run"] and not r["cause"]})
    return fill_causes(rows, run_store.attempt_causes_for(root / "runs", runs))
