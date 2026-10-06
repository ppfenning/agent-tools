"""The local forge: the forge protocol over plain git, with no pull-request host.

The land's own checks are the gate, so there is no pull request to open or wait
on. The task commit reaches the default branch by a fast-forward. The only
network use is a push of the default branch to an existing `origin`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_tools.forge import ForgeNotSupported

__all__ = ["find_open_prs", "merge", "merge_state", "open_pr", "push", "update_branch", "wait_checks"]


def _git(repo: Path | str, *args: str) -> tuple[int, str]:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.returncode, (done.stdout + done.stderr).strip()


def find_open_prs(repo: Path | str, branch: str) -> list[int] | str:
    return []


def push(repo: Path | str, branch: str) -> tuple[bool, str]:
    return True, f"local forge: {branch} stays local until it merges"


def open_pr(repo: Path | str, title: str, body: str, *, head: str | None = None, base: str | None = None) -> tuple[bool, str]:
    return True, "local forge: no pull request"


def wait_checks(repo: Path | str, timeout_s: float, *, ref: str = "HEAD") -> tuple[bool, str]:
    return True, "local forge: the land's own checks are the gate"


def merge_state(pr: int, *, repo: Path | str | None = None) -> str:
    raise ForgeNotSupported("local forge: no pull requests, so no merge state")


def update_branch(pr: int, *, repo: Path | str | None = None) -> None:
    raise ForgeNotSupported("local forge: no pull requests, so no branch to update")


def merge(repo: Path | str, step: Mapping[str, Any]) -> tuple[bool, str]:
    """Fast-forward `default_branch` to `branch`, push it if `origin` exists, drop `branch`.

    `step` is a mapping with `branch`, `default_branch` and `subject`. A repo on
    `default_branch` merges; a repo on any other branch keeps it and fast-forwards
    the ref with `fetch .`, which refuses a non-fast-forward. A repo on `branch`
    itself moves to `default_branch` first, because git will not delete a
    checked-out branch. A failure returns git's message and leaves `branch` in place.
    """
    branch, default = step["branch"], step["default_branch"]
    code, out = _git(repo, "symbolic-ref", "--short", "HEAD")
    current = out if code == 0 else None
    leave = [("checkout", default)] if current == branch else []
    advance = ("merge", "--ff-only", branch) if current in (branch, default) else ("fetch", ".", f"{branch}:{default}")
    for args in (*leave, advance):
        code, out = _git(repo, *args)
        if code != 0:
            return False, out
    if _git(repo, "remote", "get-url", "origin")[0] == 0:
        code, out = _git(repo, "push", "origin", default)
        if code != 0:
            return False, out
    code, out = _git(repo, "branch", "-D", branch)
    if code != 0:
        return False, out
    _, sha = _git(repo, "rev-parse", "--short", default)
    return True, f"merged {branch} into {default} at {sha}"
