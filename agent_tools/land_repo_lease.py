"""Repo-scoped land lease: one land per repository at a time, over store_cli's lease API.

Distinct from land_lease.py's task-scoped `land:<task>` lease. This lease is named
`land:<repo>` and is held for the duration of a land against that repository, not a task.
"""

from __future__ import annotations

from pathlib import Path

from agent_tools import store_cli


def lease_name(repo: str) -> str:
    return f"land:{repo}"


def refusal_message(repo: str, holder: str) -> str:
    return f"land: refusing, {holder} is landing in {repo}"


def acquire(runs_dir: Path, repo: str, holder: str, ttl: int) -> store_cli.LeaseResult:
    return store_cli.lease_acquire(runs_dir, lease_name(repo), holder, ttl)


def renew(runs_dir: Path, repo: str, holder: str, epoch: int, ttl: int) -> store_cli.LeaseResult:
    return store_cli.lease_renew(runs_dir, lease_name(repo), holder, epoch, ttl)


def release(runs_dir: Path, repo: str, holder: str, epoch: int) -> store_cli.LeaseResult:
    return store_cli.lease_release(runs_dir, lease_name(repo), holder, epoch)
