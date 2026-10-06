"""Revert a landed PR that turned main red: the imperative edge.

It decides nothing beyond the order of steps and reaches git and the forge only
through the two ports below. It takes plain values, not chair types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_tools.land import pr_footer

__all__ = [
    "CHECKS_FAILED",
    "CONFLICT",
    "MERGED",
    "ForgePort",
    "GitPort",
    "RevertResult",
    "perform_revert",
    "revert_body",
]

MERGED = "merged"
CONFLICT = "conflict"
CHECKS_FAILED = "checks_failed"


class GitPort(Protocol):
    def fetch_main(self, repo: str) -> None: ...

    def revert_onto_branch(self, repo: str, branch: str, commit: str) -> list[str]:
        """Branch from origin/main and `git revert --no-edit` the commit; return the conflicting files, having aborted the revert if any."""
        ...

    def push(self, repo: str, branch: str) -> None: ...


class ForgePort(Protocol):
    def open_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        """Return the PR url."""
        ...

    def wait_checks(self, pr: str) -> list[str]:
        """Return the names of failing checks; empty means green."""
        ...

    def merge(self, pr: str) -> None: ...


@dataclass(frozen=True)
class RevertResult:
    status: str
    pr: str = ""  # empty when no PR was opened
    detail: str = ""


def revert_body(reason: str, footer: str) -> str:
    return f"{reason}\n\n{footer}"


def perform_revert(repo: str, pr: int, commit: str, reason: str, git: GitPort, forge: ForgePort) -> RevertResult:
    branch = f"revert/{pr}"
    git.fetch_main(repo)
    conflicts = git.revert_onto_branch(repo, branch, commit)
    if conflicts:
        return RevertResult(CONFLICT, "", "revert conflicts in: " + ", ".join(conflicts))
    git.push(repo, branch)
    url = forge.open_pr(repo, branch, f"Revert #{pr}: main red after land", revert_body(reason, pr_footer(None)))
    failing = forge.wait_checks(url)
    if failing:
        return RevertResult(CHECKS_FAILED, url, "revert checks failed: " + ", ".join(failing))
    forge.merge(url)
    return RevertResult(MERGED, url, "")
