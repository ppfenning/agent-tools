"""The auto forge: each repository's own forge, from its `origin`.

A repository whose `origin` is on github.com lands through `forge_github`, with a
pull request and its checks. Any other repository, including one with no remote
at all, lands through `forge_local`: its own checks are the gate and the default
branch moves by a fast-forward. One profile can then hold both kinds of project.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_tools import forge_github, forge_local
from agent_tools.forge import ForgeNotSupported

__all__ = ["find_open_prs", "forge_of", "merge", "merge_state", "open_pr", "push", "update_branch", "wait_checks"]


def forge_of(repo: Path | str):
    """`forge_github` when `origin` names github.com, else `forge_local` (no origin included)."""
    done = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True)
    url = done.stdout.strip() if done.returncode == 0 else ""
    return forge_github if "github.com" in url else forge_local


def find_open_prs(repo: Path | str, branch: str) -> list[int] | str:
    return forge_of(repo).find_open_prs(Path(repo), branch)


def push(repo: Path | str, branch: str) -> tuple[bool, str]:
    return forge_of(repo).push(Path(repo), branch)


def open_pr(repo: Path | str, title: str, body: str, *, head: str | None = None, base: str | None = None) -> tuple[bool, str]:
    return forge_of(repo).open_pr(Path(repo), title, body, head=head, base=base)


def wait_checks(repo: Path | str, timeout_s: float, *, ref: str = "HEAD") -> tuple[bool, str]:
    return forge_of(repo).wait_checks(Path(repo), timeout_s, ref=ref)


def merge_state(pr: int, *, repo: Path | str | None = None) -> str:
    if repo is None:
        raise ForgeNotSupported("auto forge: a PR number alone does not name a repository, so it cannot pick a forge")
    return forge_of(repo).merge_state(pr, repo=repo)


def update_branch(pr: int, *, repo: Path | str | None = None) -> None:
    if repo is None:
        raise ForgeNotSupported("auto forge: a PR number alone does not name a repository, so it cannot pick a forge")
    forge_of(repo).update_branch(pr, repo=repo)


def merge(repo: Path | str, step: Mapping[str, Any]) -> tuple[bool, str]:
    return forge_of(repo).merge(Path(repo), step)
