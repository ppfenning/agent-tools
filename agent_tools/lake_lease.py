"""Guards `agent_tools.lake_sync.sync` with the store's `lake:sync` lease so two hosts never sync at once.

`store_cli.lease_acquire` is already non-blocking: a held lease answers `LeaseRefused` immediately, so this module
never waits, it just skips. A lease error or a missing harness (`LeaseError` / `NotAvailable`) fails open, the same
posture `chair.acquire_lease` takes: a store outage must never block the sync it exists to protect. A granted lease
is always released in a `finally`, pass or fail, and exactly one retry absorbs an Iceberg `CommitFailedException`
since `sync()` re-reads each table's high-water mark from the table property on every call.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_tools import store_cli

__all__ = ["SkippedError", "SkippedHeld", "SyncOutcome", "Synced", "sync_under_lease"]

LEASE_NAME = "lake:sync"


@dataclass(frozen=True)
class Synced[T]:
    value: T


@dataclass(frozen=True)
class SkippedHeld:
    holder: str | None


@dataclass(frozen=True)
class SkippedError:
    name: str


type SyncOutcome[T] = Synced[T] | SkippedHeld | SkippedError


class _NeverMatches(Exception):
    """A sentinel exception type that no real error is ever an instance of."""


def _commit_failed_exception() -> type[Exception]:
    try:
        from pyiceberg.exceptions import CommitFailedException
    except ImportError:
        return _NeverMatches
    return CommitFailedException


def _holder() -> str:
    return f"pid {os.getpid()} on {socket.gethostname()}"


def sync_under_lease[T](sync: Callable[[], T], runs_dir: Path, ttl: int = 900) -> SyncOutcome[T]:
    """Edge. Runs `sync()` while holding the store's `lake:sync` lease; skips it when another holder has the lease."""
    holder = _holder()
    result = store_cli.lease_acquire(runs_dir, LEASE_NAME, holder, ttl)
    if isinstance(result, store_cli.LeaseRefused):
        return SkippedHeld(result.holder)
    if not isinstance(result, store_cli.LeaseGranted):
        return Synced(sync())
    commit_failed_exception = _commit_failed_exception()
    try:
        try:
            value = sync()
        except commit_failed_exception:
            value = sync()
    except Exception as e:
        return SkippedError(type(e).__name__)
    finally:
        store_cli.lease_release(runs_dir, LEASE_NAME, holder, result.epoch)
    return Synced(value)
