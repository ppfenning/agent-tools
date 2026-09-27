"""Chair reader for `stranded`: the rows of `runs_stranded.stranded`, projected to five keys."""

from __future__ import annotations

import os
from collections.abc import Callable

from agent_tools import runs_stranded

KEYS = ("run", "task", "phase", "branch", "remedy")


def keep_stranded_keys(rows: list[dict]) -> list[dict]:
    """Plain dicts holding only `KEYS`, in input order."""
    return [{k: row.get(k) for k in KEYS} for row in rows]


def read_stranded(records: list[dict], items: list[dict],
                   repo_exists: Callable[[str], bool] = os.path.isdir) -> list[dict]:
    """Edge. `records` and `items` come from the caller: their loaders live in
    cli.py, which this module does not import. `repo_exists` defaults to a
    real directory check so a dropped item or a vanished repository never
    reaches the chair planner as needs_chair; tests inject a fake."""
    return keep_stranded_keys(runs_stranded.stranded(records, items, repo_exists))


def read_missing_repos(records: list[dict], items: list[dict],
                        repo_exists: Callable[[str], bool] = os.path.isdir) -> list[str]:
    """Edge. The repo paths `read_stranded` is skipping because `repo_exists` rejects them."""
    return runs_stranded.missing_repos(records, items, repo_exists)
