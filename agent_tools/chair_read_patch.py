"""Whether a quarantined task record kept a non-blank `build.patch`: the reader behind the `has_patch` fact."""

from __future__ import annotations

from pathlib import Path

from agent_tools import run_store

__all__ = ["has_patch", "read_has_patch"]


def has_patch(record: dict | None) -> bool:
    """True only when `record["build"]["patch"]` is a string holding a non-whitespace character."""
    build = record.get("build") if isinstance(record, dict) else None
    patch = build.get("patch") if isinstance(build, dict) else None
    return isinstance(patch, str) and bool(patch.strip())


def read_has_patch(runs_dir: Path, run_id: str, task: str) -> bool:
    """Edge: `has_patch` of the store record for the run's task. False unless exactly one phase holds that task."""
    phases = run_store.task_record_phases(runs_dir, run_id, task)
    return has_patch(run_store.task_record(runs_dir, run_id, phases[0], task) if len(phases) == 1 else None)
