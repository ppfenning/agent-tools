"""Land worker: starts a land on a background thread and returns at once, one land per repository.

The worker holds no repo lease. `cox runs land` takes `land:<repo>` itself, as its own holder, and that lease is the
guard across processes. A lease the worker took first made that subprocess refuse every land against its own chair
(tools #1290). The worker keeps only in-memory bookkeeping, which repos it is landing in and the queue behind them, so
two lands in one repository never overlap, plus a beat of the chair lease while any land runs.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


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
class LandResult[T]:
    value: T | None
    error: BaseException | None
    warning: str | None


class LandRefused(Exception):
    pass


def may_start(running_here: bool, queued: int) -> Started | Queued:
    """Queue behind this worker's own land in the repository; otherwise start."""
    return Queued(queued + 1) if running_here else Started()


def beat_due(now: float, last_beat: float | None, interval: float, running: int) -> bool:
    return running > 0 and (last_beat is None or now - last_beat >= interval)


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


Land = Callable[[], object]


class LandWorker:
    def __init__(
        self, beat: Callable[[], None], interval: float, clock: Callable[[], float], wait: Callable[[float], None],
    ) -> None:
        """`beat` renews the chair lease once per `interval` while any land runs."""
        self._beat, self._interval, self._clock, self._wait = beat, interval, clock, wait
        self._mutex = threading.Lock()
        self._running: set[str] = set()
        self._queues: dict[str, deque[tuple[Land, LandHandle]]] = {}
        self._beating = False
        self._stopped = False
        self.beat_errors: tuple[BaseException, ...] = ()

    def submit[T](self, repo: str, land: Callable[[], T]) -> Submitted[T]:
        handle: LandHandle[T] = LandHandle()
        with self._mutex:
            if self._stopped:
                return Submitted(Refused(None), None)
            if repo in self._running:
                queue = self._queues.setdefault(repo, deque())
                outcome = may_start(True, len(queue))
                queue.append((land, handle))
                return Submitted(outcome, handle)
            self._running.add(repo)
            if not self._beating:
                self._beating = True
                threading.Thread(target=self._beat_loop, daemon=True).start()
        threading.Thread(target=self._land_loop, args=(repo, land, handle), daemon=True).start()
        return Submitted(may_start(False, 0), handle)

    def _land_loop(self, repo: str, land: Land, handle: LandHandle) -> None:
        handle._finish(self._execute(land))
        for queued_land, queued_handle in iter(lambda: self._next_queued(repo), None):
            queued_handle._finish(self._execute(queued_land))

    def _next_queued(self, repo: str) -> tuple[Land, LandHandle] | None:
        with self._mutex:
            queue = self._queues.get(repo)
            if queue:
                return queue.popleft()
            self._queues.pop(repo, None)
            self._running.discard(repo)
            return None

    def _execute(self, land: Land) -> LandResult:
        """Edge. Never raises: the land's failure, of any kind, is the result's error."""
        try:
            return LandResult(land(), None, None)
        except BaseException as exc:
            return LandResult(None, exc, None)

    def stop(self) -> None:
        """Queued lands finish refused and no later land starts; a land still running is left to finish."""
        with self._mutex:
            self._stopped = True
            queued = tuple(handle for queue in self._queues.values() for _, handle in queue)
            self._queues = {}
        for handle in queued:
            handle._finish(LandResult(None, LandRefused("chair stopped"), None))

    def _beat_loop(self) -> None:
        last: float | None = None
        while True:
            with self._mutex:
                running = len(self._running)
                if running == 0 or self._stopped:
                    self._beating = False
                    return
            now = self._clock()
            if beat_due(now, last, self._interval, running):
                try:
                    self._beat()
                except Exception as exc:
                    self.beat_errors = (*self.beat_errors, exc)
                last = now
            self._wait(self._interval)
