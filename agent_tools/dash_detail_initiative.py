"""Data behind `cox dash --detail initiative <id>`: an initiative's phases, the
needs edges between its tasks, each task and its state, each phase's land time
and the runs that touched it with their cost. `_items` reads `run_store.work_items`,
the store reader `route status`'s `_stored_work_items` and the console
phase-progress panel's `_work_items_snapshot` both call — a task's `updated_at`
lives only in that store, never in its frontmatter file. `_runs` reads
`run_store.usages` (local usage files unioned with ended store-only runs) and
keeps the ones whose id names this initiative, the same derivation
`chair_facts.run_initiative` gives `cox runs top` its lane-to-initiative
correlation. `shape` is the pure core: given plain item and run-record lists it
never touches a filesystem, a clock or a subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_tools import chair_facts, records, run_store

__all__ = ["build", "shape"]

_LANDED_STATES = ("done", "dropped")  # same rule console_screen.py applies per lane


def _needs_of(raw: object) -> list:
    """A `needs_json` store cell decoded to a plain list: already a list when the
    driver decoded it (a Postgres JSONB column), JSON text when it did not
    (SQLite), `[]` for anything else. Never raises.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _items(runs_dir: Path, initiative_id: str) -> list[dict]:
    """One item per `run_store.work_items` row for this initiative, its
    `task_id` renamed `id` and its `needs_json` decoded to `needs`; `state`
    and `updated_at` pass through unchanged, since the store is the only
    place either is recorded.
    """
    return [
        {
            "id": row.get("task_id"),
            "phase": row.get("phase"),
            "state": row.get("state"),
            "needs": _needs_of(row.get("needs_json")),
            "updated_at": row.get("updated_at"),
        }
        for row in run_store.work_items(runs_dir, initiative_id)
    ]


def _runs(runs_dir: Path, initiative_id: str) -> list[dict]:
    """One `{"run", "cost_usd"}` row per run whose id names this initiative
    (`chair_facts.run_initiative`), discovered through `run_store.usages` —
    local usage files unioned with ended runs recorded only in the store — its
    cost the total of that usage (`records.usage_summary`).
    """
    return [
        {"run": run_id, "cost_usd": records.usage_summary(usage)["cost_usd"]}
        for run_id, usage in run_store.usages(runs_dir).items()
        if chair_facts.run_initiative(run_id) == initiative_id
    ]


def _phase_landed_at(items: list[dict], phase: str) -> str | None:
    """The land time of `phase`: the latest `updated_at` among its own items,
    invented from nothing else, when every one of them is `done` or `dropped`
    (`_LANDED_STATES`); `None` while any item in the phase is still open, or
    while a landed phase's items carry no `updated_at` at all.
    """
    own = [item for item in items if item.get("phase") == phase]
    if not own or any(item.get("state") not in _LANDED_STATES for item in own):
        return None
    stamps = [item.get("updated_at") for item in own if item.get("updated_at") is not None]
    return max(stamps) if stamps else None


def shape(items: list[dict], runs: list[dict], now: str) -> dict:
    """The `cox dash --detail initiative` snapshot for a plain `items` list (one
    initiative's work items, each carrying at least `id`, `phase`, `state`,
    `needs`, `updated_at`) and a plain `runs` list (each carrying `run`,
    `cost_usd`). A `needs` edge points `from` the dependency `to` the item that
    names it in its own `needs` list. `now` is carried through as
    `generated_at`, this snapshot's own timestamp; nothing here reads the clock.
    """
    phases = sorted({item["phase"] for item in items})
    needs = sorted(
        ({"from": needed, "to": item["id"]} for item in items for needed in item.get("needs") or []),
        key=lambda edge: (edge["from"], edge["to"]),
    )
    return {
        "generated_at": now,
        "phases": [{"name": phase, "landed_at": _phase_landed_at(items, phase)} for phase in phases],
        "needs": needs,
        "tasks": [{"id": item["id"], "phase": item["phase"], "state": item["state"]} for item in items],
        "runs": sorted(runs, key=lambda row: row["run"]),
    }


def build(initiative_id: str, work_dir: Path, runs_dir: Path, now: str) -> dict:
    """The `shape` snapshot for one initiative. `work_dir` is part of this
    module's public signature but unused: a task's state and land time live
    only in the store `_items` reads, never in its frontmatter file, so both
    `_items` and `_runs` read `runs_dir` alone.
    """
    return shape(_items(runs_dir, initiative_id), _runs(runs_dir, initiative_id), now)
