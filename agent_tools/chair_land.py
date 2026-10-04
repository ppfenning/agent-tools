"""Land worker: starts a land on a background thread and returns at once, one land per repository.

The repo lease in land_repo_lease is the source of truth for one-at-a-time. The worker takes it, renews it on
every beat, and hands it to the land callable as a RepoLease, so the land must use that lease and not acquire
`land:<repo>` a second time under another holder: a second acquire would refuse the land against its own worker.
The worker's mutex only guards its bookkeeping (which repos it is landing in, the queue behind them) and is never
held across a lease call.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from agent_tools import land_repo_lease, store_cli


@dataclass(frozen=True)
class Started:
    pass


@dataclass(frozen=True)
class Queued:
    position: int


@dataclass(frozen=True)
class Refused:
    holder: str | None


@dataclass(frozen=True)
class RepoLease:
    """`epoch` is None when the store could not answer and the land walks without the lease, as cli's land does."""

    repo: str
    holder: str
    epoch: int | None


@dataclass(frozen=True)
class LandResult[T]:
    value: T | None
    error: BaseException | None
    warning: str | None


class LandRefused(Exception):
    pass


def may_start(running_here: bool, queued: int, lease: store_cli.LeaseResult | None) -> Started | Queued | Refused:
    """Queue behind this worker's own land; refuse only a lease held elsewhere; a store outage fails open."""
    if running_here:
        return Queued(queued + 1)
    if isinstance(lease, store_cli.LeaseRefused):
        return Refused(lease.holder)
    return Started()


def beat_due(now: float, last_beat: float | None, interval: float, running: int) -> bool:
    return running > 0 and (last_beat is None or now - last_beat >= interval)


def renewals(running: Mapping[str, RepoLease | None]) -> tuple[RepoLease, ...]:
    return tuple(lease for lease in running.values() if lease is not None and lease.epoch is not None)


def granted_lease(repo: str, holder: str, lease: store_cli.LeaseResult) -> RepoLease:
    return RepoLease(repo, holder, lease.epoch if isinstance(lease, store_cli.LeaseGranted) else None)


class LandHandle[T]:
    def __init__(self) -> None:
        self._done = threading.Event()
        self._result: LandResult[T] | None = None

    def in_progress(self) -> bool:
        return not self._done.is_set()

    def result(self) -> LandResult[T] | None:
        return self._result

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def _finish(self, result: LandResult[T]) -> None:
        self._result = result
        self._done.set()


@dataclass(frozen=True)
class Submitted[T]:
    outcome: Started | Queued | Refused
    handle: LandHandle[T] | None


Land = Callable[[RepoLease], object]


def _warning(repo: str, verb: str, result: object) -> str:
    return f"warning: land lease {land_repo_lease.lease_name(repo)} not {verb}: {result!r}"


def _refused(repo: str, holder: str | None) -> LandResult:
    return LandResult(None, LandRefused(land_repo_lease.refusal_message(repo, holder or "another land")), None)


class LandWorker:
    def __init__(
        self,
        runs_dir: Path,
        holder: str,
        ttl: int,
        beat: Callable[[], None],
        interval: float,
        clock: Callable[[], float],
        wait: Callable[[float], None],
    ) -> None:
        """`interval` must be shorter than `ttl`: the lease is renewed once per beat."""
        self._runs_dir, self._holder, self._ttl = runs_dir, holder, ttl
        self._beat, self._interval, self._clock, self._wait = beat, interval, clock, wait
        self._mutex = threading.Lock()
        # None marks a repo whose lease call is in flight.
        self._running: dict[str, RepoLease | None] = {}
        self._queues: dict[str, deque[tuple[Land, LandHandle]]] = {}
        self._warnings: dict[str, tuple[str, ...]] = {}
        self._beating = False
        self.beat_errors: tuple[BaseException, ...] = ()

    def submit[T](self, repo: str, land: Callable[[RepoLease], T]) -> Submitted[T]:
        handle: LandHandle[T] = LandHandle()
        with self._mutex:
            if repo in self._running:
                queue = self._queues.setdefault(repo, deque())
                outcome = may_start(True, len(queue), None)
                queue.append((land, handle))
                return Submitted(outcome, handle)
            self._running[repo] = None
        got = self._acquire(repo)
        outcome = may_start(False, 0, got)
        if isinstance(outcome, Refused):
            for _, queued in self._abandon(repo):
                queued._finish(_refused(repo, outcome.holder))
            return Submitted(outcome, None)
        lease = granted_lease(repo, self._holder, got)
        with self._mutex:
            self._running[repo] = lease
            if not self._beating:
                self._beating = True
                threading.Thread(target=self._beat_loop, daemon=True).start()
        threading.Thread(target=self._land_loop, args=(lease, land, handle), daemon=True).start()
        return Submitted(outcome, handle)

    def _acquire(self, repo: str) -> store_cli.LeaseResult:
        """Edge. An exception from the store is an outage, and an outage fails open."""
        try:
            return land_repo_lease.acquire(self._runs_dir, repo, self._holder, self._ttl)
        except Exception as exc:
            return store_cli.LeaseError(f"{type(exc).__name__}: {exc}")

    def _abandon(self, repo: str) -> tuple[tuple[Land, LandHandle], ...]:
        with self._mutex:
            self._running.pop(repo, None)
            self._warnings.pop(repo, None)
            return tuple(self._queues.pop(repo, deque()))

    def _land_loop(self, lease: RepoLease, land: Land, handle: LandHandle) -> None:
        handle._finish(self._execute(lease, land))
        for queued_land, queued_handle in iter(lambda: self._next_queued(lease.repo), None):
            queued_handle._finish(self._run_queued(lease.repo, queued_land))

    def _next_queued(self, repo: str) -> tuple[Land, LandHandle] | None:
        with self._mutex:
            queue = self._queues.get(repo)
            if queue:
                self._running[repo] = None
                return queue.popleft()
            self._queues.pop(repo, None)
            self._running.pop(repo, None)
            return None

    def _run_queued(self, repo: str, land: Land) -> LandResult:
        got = self._acquire(repo)
        outcome = may_start(False, 0, got)
        if isinstance(outcome, Refused):
            return _refused(repo, outcome.holder)
        lease = granted_lease(repo, self._holder, got)
        with self._mutex:
            self._running[repo] = lease
        return self._execute(lease, land)

    def _execute(self, lease: RepoLease, land: Land) -> LandResult:
        """Edge. Never raises: the land's failure, of any kind, is the result's error."""
        try:
            value, error = land(lease), None
        except BaseException as exc:
            value, error = None, exc
        with self._mutex:
            current = self._running.get(lease.repo) or lease
            # Back to in-flight before the release, so a beat cannot renew a lease being given up.
            self._running[lease.repo] = None
            renew_warnings = self._warnings.pop(lease.repo, ())
        warnings = (*renew_warnings, *((self._release(current),) if current.epoch is not None else ()))
        return LandResult(value, error, "\n".join(w for w in warnings if w) or None)

    def _release(self, lease: RepoLease) -> str | None:
        try:
            got = land_repo_lease.release(self._runs_dir, lease.repo, lease.holder, lease.epoch)
        except Exception as exc:
            return _warning(lease.repo, "released", exc)
        return None if isinstance(got, store_cli.LeaseReleased) else _warning(lease.repo, "released", got)

    def _renew(self, lease: RepoLease) -> None:
        try:
            got = land_repo_lease.renew(self._runs_dir, lease.repo, lease.holder, lease.epoch, self._ttl)
        except Exception as exc:
            got = store_cli.LeaseError(f"{type(exc).__name__}: {exc}")
        with self._mutex:
            if self._running.get(lease.repo) != lease:
                return
            if isinstance(got, store_cli.LeaseGranted):
                self._running[lease.repo] = replace(lease, epoch=got.epoch)
            else:
                self._warnings[lease.repo] = (*self._warnings.get(lease.repo, ()), _warning(lease.repo, "renewed", got))

    def _beat_loop(self) -> None:
        last: float | None = None
        while True:
            with self._mutex:
                running = len(self._running)
                leases = renewals(self._running)
                if running == 0:
                    self._beating = False
                    return
            now = self._clock()
            if beat_due(now, last, self._interval, running):
                try:
                    self._beat()
                except Exception as exc:
                    self.beat_errors = (*self.beat_errors, exc)
                for lease in leases:
                    self._renew(lease)
                last = now
            self._wait(self._interval)
