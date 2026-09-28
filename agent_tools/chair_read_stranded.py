"""Chair reader for `stranded`: the rows of `runs_stranded.stranded`, projected to five keys."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

from agent_tools import runs_stranded

KEYS = ("run", "task", "phase", "branch", "remedy")


def keep_stranded_keys(rows: list[dict]) -> list[dict]:
    """Plain dicts holding only `KEYS`, in input order."""
    return [{k: row.get(k) for k in KEYS} for row in rows]


def _branch_exists(repo: str, branch: str) -> bool:
    """A real, git-backed check: whether `branch` is present as a local branch of `repo`."""
    r = subprocess.run(["git", "-C", repo, "branch", "--list", branch], capture_output=True, text=True)
    return bool(r.stdout.strip())


def read_stranded(records: list[dict], items: list[dict],
                   repo_exists: Callable[[str], bool] = os.path.isdir,
                   branch_exists: Callable[[str, str], bool] = _branch_exists) -> list[dict]:
    """Edge. `records` and `items` come from the caller: their loaders live in
    cli.py, which this module does not import. `repo_exists` defaults to a
    real directory check so a dropped item or a vanished repository never
    reaches the chair planner as needs_chair; `branch_exists` defaults to a
    real git check so a task on a still-carried phase branch stops reaching
    the chair as a stranded quarantine; tests inject fakes for both."""
    return keep_stranded_keys(runs_stranded.stranded(records, items, repo_exists, branch_exists))


def read_missing_repos(records: list[dict], items: list[dict],
                        repo_exists: Callable[[str], bool] = os.path.isdir) -> list[str]:
    """Edge. The repo paths `read_stranded` is skipping because `repo_exists` rejects them."""
    return runs_stranded.missing_repos(records, items, repo_exists)


def stranded_from_rows(rows: list[dict], records: list[dict]) -> list[dict]:
    """The same rule as `read_stranded`, with each task's state taken from `rows` (kind "task") instead of files."""
    items = [
        {"id": row.get("task_id"), "initiative": row.get("initiative"), "phase": row.get("phase"), "state": row.get("state")}
        for row in rows
        if row.get("kind") == "task"
    ]
    return keep_stranded_keys(runs_stranded.stranded(records, items))
