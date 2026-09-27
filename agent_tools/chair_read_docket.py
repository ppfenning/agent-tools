"""Readers for the chair's `docket` and `work_store_ready` sources.

The builder is `route.initiative_summaries`, the initiatives rows behind `route context`
(`route.context_document` assembles them into the `--json` doc). Its rows are {id, phase, ready,
awaiting_merge?}, so the ids, needs and landed sets FactsDeps asks for come from the same work
items the builder was given. The builder itself is called, never copied.

unknown: `max_in_flight` has no source in the profile or the pacing policy, so the caller passes it.
unknown: `started` is taken as any task in the initiative already in_progress, approved, done or dropped.
"""
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import epic, route, run_store

Row = Mapping[str, Any]

_BEGUN = frozenset({"in_progress", "approved"}) | route.TERMINAL


def _initiative_row(summary: Row, items: Sequence[Row]) -> dict[str, Any]:
    own = [i for i in items if i["initiative"] == summary["id"]]
    landed = {i["id"] for i in own if i["state"] in route.TERMINAL}
    ready = [
        i for i in own
        if summary["ready"] and i["phase"] == summary["phase"] and i["state"] == "ready" and set(i["needs"]) <= landed
    ]
    return {
        "id": summary["id"],
        "started": any(i["state"] in _BEGUN for i in own),
        "ready_tasks": [{"id": i["id"], "needs": list(i["needs"])} for i in ready],
        "landed": landed,
    }


def docket_from_builder(summaries: Sequence[Row], items: Sequence[Row], busy_lanes: int, max_in_flight: int) -> dict[str, Any]:
    """`summaries` is `route.initiative_summaries(items)`; `items` are the work items it was built from."""
    return {
        "initiatives": [_initiative_row(s, items) for s in summaries],
        "busy_lanes": busy_lanes,
        "max_in_flight": max_in_flight,
    }


def work_store_ready(docket: Row) -> bool:
    return any(i["ready_tasks"] for i in docket["initiatives"])


def _claimed(row: Row, now: str) -> bool:
    return bool(row["holder"]) and row["expires_at"] > now


def _item_of(row: Row) -> dict[str, Any]:
    return {"id": row["task_id"], "initiative": row["initiative"], "phase": row["phase"], "state": row["state"], "needs": list(row["needs"])}


def docket_from_rows(rows: Sequence[Row], now: str) -> list[dict[str, Any]]:
    """The builder's `initiatives` over the task rows, less each task a live lease holds (`holder` set, `expires_at` after ISO `now`)."""
    tasks = [r for r in rows if r["kind"] == "task"]
    items = [_item_of(r) for r in tasks]
    claimed = {(r["initiative"], r["task_id"]) for r in tasks if _claimed(r, now)}
    return [
        {**i, "ready_tasks": [t for t in i["ready_tasks"] if (i["id"], t["id"]) not in claimed]}
        for i in (_initiative_row(s, items) for s in route.initiative_summaries(items))
    ]


def _text(p: Path) -> str | None:
    try:
        return p.read_text()
    except OSError:
        return None


def _work_items(ws: Path, mode: str) -> list[dict]:
    """Every work item under `work/<initiative>/<phase>/<task>.md`, its state from the store under mode "store"."""
    items = [
        route.work_item(route.parse_frontmatter(text)[0], initiative=p.parent.parent.name, phase_dir=p.parent.name, stem=p.stem)
        for p in sorted((ws / "work").glob("*/*/*.md"))
        if p.name != "initiative.md" and (text := _text(p)) is not None
    ]
    return route.with_store_states(items, run_store.work_items(ws / "runs") if mode == "store" else [], mode)


def local_runs(runs_dir: Path, now: str) -> tuple[int, set[str]]:
    """Edge. How many local pidfile runs are live, and every run a local pidfile names."""
    local = {p.stem: route.parse_pid(t) for p in sorted(runs_dir.glob("*.pid")) if (t := _text(p)) is not None}
    live = [rid for rid, pid in local.items() if epic.run_live(pid, runs_dir / f"{rid}.pid", now=now)]
    return len(live), set(local)


def _busy_lanes(runs_dir: Path, now: str) -> int:
    """Runs with a live local pidfile, plus live store leases no local pidfile names."""
    live, named = local_runs(runs_dir, now)
    return live + len(run_store.remote_lanes(run_store.live_lanes(runs_dir, now), named))


def read_docket(ws: Path, mode: str, max_in_flight: int, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> dict[str, Any]:
    """Edge: the builder over the workspace `ws` and its store. `mode` is `work_state.work_state_mode`'s answer."""
    items = _work_items(ws, mode)
    busy = _busy_lanes(ws / "runs", now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    return docket_from_builder(route.initiative_summaries(items), items, busy, max_in_flight)


def docket_source(ws: Path, mode: str, max_in_flight: int) -> Callable[[], dict[str, Any]]:
    return lambda: read_docket(ws, mode, max_in_flight)


def work_store_ready_source(ws: Path, mode: str, max_in_flight: int) -> Callable[[], bool]:
    return lambda: work_store_ready(read_docket(ws, mode, max_in_flight))
