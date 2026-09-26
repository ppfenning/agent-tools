"""The `approved` source of a chair tick: approved work items with phase_done and needs.

`approved_rows` is pure. `read_approved` is the edge: it loads items through the existing work-store readers
(`route.parse_frontmatter`, `route.work_item`, `route.with_store_states`, `run_store.work_items`) and hands them over.
"""
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import route, run_store
from agent_tools.chair_facts import run_initiative
from agent_tools.chair_types import ApprovedTask
from agent_tools.remote_lane import fetched_record_path, land_needs_fetch, remote_record_path

Item = Mapping[str, Any]


def phase_done(item: Item, items: Sequence[Item]) -> bool:
    """True when every other task in the item's initiative and phase is done; a lone task is True."""
    return all(
        other["state"] == "done"
        for other in items
        if other is not item
        and other["initiative"] == item["initiative"]
        and other["phase"] == item["phase"]
    )


def approved_rows(items: Sequence[Item]) -> list[ApprovedTask]:
    """One row per item whose state is `approved`. `repo` is the item's own, `""` when the caller found none."""
    return [
        {
            "id": item["id"],
            "initiative": item["initiative"],
            "repo": item.get("repo", ""),
            "phase": item["phase"],
            "phase_done": phase_done(item, items),
            "needs": list(item.get("needs", [])),
            "run": "",
            "needs_fetch": False,
        }
        for item in items
        if item["state"] == "approved"
    ]


def _run_order(run: str) -> tuple[int, str]:
    """`x-10` sorts after `x-2`: by the numeric suffix, then the name."""
    tail = run.rsplit("-", 1)[-1]
    return (int(tail) if tail.isdigit() else -1, run)


def with_runs(
    tasks: Sequence[ApprovedTask],
    stranded: Sequence[Mapping[str, Any]],
    fetch_facts: Mapping[str, tuple[bool, bool]] = {},
) -> list[ApprovedTask]:
    """Each task gets the newest run with a stranded row for it under its initiative and phase, empty when none has.

    `fetch_facts` maps run -> (has_remote_record, fetched); `needs_fetch` is False for a run it does not list.
    """
    runs = {
        (run_initiative(str(row["run"])), str(row["phase"]), str(row["task"])): str(row["run"])
        for row in sorted(stranded, key=lambda s: _run_order(str(s["run"])))
    }
    return [
        {**t, "run": run, "needs_fetch": bool(run) and land_needs_fetch(*fetch_facts.get(run, (False, False)))}
        for t in tasks
        for run in [runs.get((t["initiative"], t["phase"], t["id"]), "")]
    ]


def read_fetch_facts(runs_dir: Path, stranded: Sequence[Mapping[str, Any]]) -> dict[str, tuple[bool, bool]]:
    """Edge. run -> (has_remote_record, fetched) for every run a stranded row names."""
    return {
        run: (remote_record_path(runs_dir, run).exists(), fetched_record_path(runs_dir, run).exists())
        for run in {str(row["run"]) for row in stranded}
        if run
    }


def _repo_of(initiative_md: Path) -> str:
    try:
        text = initiative_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    return str(route.parse_frontmatter(text)[0].get("repo", ""))


def _load_items(ws: Path) -> list[dict]:
    repos: dict[str, str] = {}
    items = []
    for path in sorted((ws / "work").glob("*/*/*.md")):
        if path.name == "initiative.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        initiative = path.parent.parent.name
        repos.setdefault(initiative, _repo_of(path.parent.parent / "initiative.md"))
        item = route.work_item(
            route.parse_frontmatter(text)[0], initiative=initiative, phase_dir=path.parent.name, stem=path.stem
        )
        items.append({"repo": repos[initiative], **item})
    return items


def read_approved(ws: Path, mode: str = "files") -> list[ApprovedTask]:
    """Edge. Approved rows for the work store under `ws`; under mode "store" each state comes from the run store."""
    items = _load_items(ws)
    rows = run_store.work_items(ws / "runs") if mode == "store" else []
    return approved_rows(route.with_store_states(items, rows, mode))
