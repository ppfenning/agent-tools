"""The chair tick loop: beat, gather, plan, perform, report and sleep, all through injected deps."""
from __future__ import annotations

import contextlib
import json
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_tools import chair_exec, chair_land, chair_report, land_repo_lease, usage_meter
from agent_tools.chair_exec import Result
from agent_tools.chair_facts import FactsDeps, gather_facts
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_report import EASTERN, format_status, write_status
from agent_tools.chair_types import Action, Facts, PlanTick

__all__ = [
    "DEFAULT_INTERVAL", "LAND_BEAT_INTERVAL", "RunDeps", "WorkerLands", "as_holder", "error_line", "finished_land", "land_sink",
    "no_lands_to_stop", "run", "tick",
]

DEFAULT_INTERVAL = 60.0
LAND_BEAT_INTERVAL = 30.0  # half a minute, so the chair lease is beaten at least once a minute while a land runs
LAND_LEASE_TTL = 1200  # the ttl `cox runs land` itself takes its repo lease with

Gather = Callable[[FactsDeps, datetime], Facts]
Perform = Callable[[list[Action], chair_exec.Deps, Callable[[], int], bool], list[Result]]


def _read_meter_doc() -> dict | None:
    """The raw JSON of the local meter file, so a fresh reading forwards unchanged; None when `usage_meter.read` finds none."""
    if usage_meter.read() is None:
        return None
    try:
        doc = json.loads(usage_meter.DEFAULT_PATH.read_text())
    except (OSError, ValueError):  # ValueError covers JSONDecodeError and UnicodeDecodeError
        return None
    return doc if isinstance(doc, dict) else None


def no_lands_to_stop() -> None:
    """The stop hook of a chair with no land worker."""


def sigterm_as_interrupt() -> Callable[[], None]:
    """Edge. SIGTERM raises KeyboardInterrupt in the main thread; the callable returned puts the old handler back."""

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, interrupt)
    except ValueError:  # signal handlers can only be set from the main thread
        return lambda: None
    return lambda: signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


@dataclass(frozen=True)
class RunDeps:
    """`beat` renews the store lease; a lost renewal reaches the tick through the lease that `gather` reads next."""

    facts_deps: FactsDeps
    exec_deps: chair_exec.Deps
    report_deps: chair_report.Deps
    beat: Callable[[], object]
    current_epoch: Callable[[], int]
    holds: Callable[[], bool]
    release: Callable[[], None]
    sleep: Callable[[float], None]
    now: Callable[[], datetime]
    gather: Gather = gather_facts
    plan: PlanTick = plan_tick
    perform: Perform = chair_exec.perform
    meter_doc: Callable[[], dict | None] = _read_meter_doc
    stop_lands: Callable[[], object] = no_lands_to_stop
    install_sigterm: Callable[[], Callable[[], None]] = sigterm_as_interrupt


def finished_land(action: Action, outcome: chair_land.LandResult[Result] | None) -> Result:
    """The tick result of a land the worker has finished; a land that raised, or lost its repo, is a result and not an error."""
    if outcome is None:
        return {"action": action, "status": "failed", "reason": "land finished without a result"}
    if isinstance(outcome.error, chair_land.LandRefused):
        return {"action": action, "status": "busy", "reason": str(outcome.error)}
    if outcome.error is not None or outcome.value is None:
        return {"action": action, "status": "failed", "reason": f"{type(outcome.error).__name__}: {outcome.error}"}
    value = outcome.value
    return {**value, "reason": f"{value['reason']}\n{outcome.warning}"} if outcome.warning else value


class WorkerLands:
    """Edge. Lands handed to a LandWorker, one handle per repository, held until the next tick collects them."""

    def __init__(self, worker: chair_land.LandWorker) -> None:
        self._worker = worker
        self._handles: dict[str, tuple[Action, chair_land.LandHandle[Result]]] = {}

    def pending(self, repo: str) -> bool:
        return repo in self._handles

    def submit(self, action: Action, work: Callable[[], Result]) -> Result:
        repo = action.get("repo", "")
        submitted = self._worker.submit(repo, lambda _lease: work())
        if submitted.handle is None:
            holder = submitted.outcome.holder if isinstance(submitted.outcome, chair_land.Refused) else None
            reason = land_repo_lease.refusal_message(repo, holder or "another land")
            return {"action": action, "status": "busy", "reason": reason}
        self._handles[repo] = (action, submitted.handle)
        return {"action": action, "status": "in_progress", "reason": f"landing in {repo}"}

    def stop(self) -> tuple[str, ...]:
        """Releases every repository lease the worker holds; a land still running is left to finish."""
        return self._worker.stop()

    def collect(self) -> list[Result]:
        done = [repo for repo, (_, handle) in self._handles.items() if not handle.in_progress()]
        return [finished_land(action, handle.result()) for action, handle in (self._handles.pop(repo) for repo in done)]


def land_sink(
    runs_dir: Path, holder: str, beat: Callable[[], None], clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> WorkerLands:
    """Edge. The worker beats the chair lease through `beat` every LAND_BEAT_INTERVAL while any land runs."""
    return WorkerLands(chair_land.LandWorker(runs_dir, holder, LAND_LEASE_TTL, beat, LAND_BEAT_INTERVAL, clock, wait))


def error_line(exc: Exception, now: datetime, results: Sequence[Result] = ()) -> str:
    """Eastern time, as `format_status` prints it; results already performed are named so none go unreported."""
    done = ", ".join(f"{r['action'].get('kind')}:{r['status']}" for r in results)
    tail = f" | performed: {done}" if done else ""
    return f"chair {now.astimezone(EASTERN):%m-%d %H:%M %Z} | tick error: {type(exc).__name__}: {exc}{tail}"


def as_holder(facts: Facts) -> Facts:
    """The facts as seen once `take_lease` had won a released or stale lease; a live foreign holder is left alone."""
    lease = facts["lease"]
    won = lease["released"] or lease["stale"]
    return {**facts, "lease": {**lease, "mine": True}} if won else facts


def _publish_meter(deps: RunDeps, dry_run: bool, now: datetime) -> None:
    """Record the local meter reading as a `meter` action when it is fresh; nothing raised here leaves the tick."""
    if dry_run:
        return
    with contextlib.suppress(Exception):
        doc = deps.meter_doc()
        meter = None if doc is None else usage_meter.parse(doc)
        if meter is None or not usage_meter.fresh(meter, now):
            return
        # `status` is required: the store's record-action refuses a line without ts, epoch, kind and status.
        deps.exec_deps.record({
            "kind": "meter", "status": "recorded",
            "five_hour": doc["five_hour"], "seven_day": doc["seven_day"], "observed_at": doc["observed_at"],
        })


def _publish_status(deps: RunDeps, dry_run: bool, line: str, now: datetime) -> None:
    """Record the tick's status line as a `status` action, so the feed reads it from the store on any machine."""
    if dry_run or not line.strip():
        return
    with contextlib.suppress(Exception):
        deps.exec_deps.record({"kind": "status", "status": "recorded", "line": line, "at": now.isoformat()})


def tick(deps: RunDeps, dry_run: bool, now: datetime) -> str:
    """Beat first, then gather, plan, perform and format; a failure after perform still names what was performed.

    A dry run never takes the lease, so it plans as the holder would to show the actions a live tick would take.
    The meter is published right after the beat, so a gather, plan or perform that raises cannot skip it.
    """
    deps.beat()
    _publish_meter(deps, dry_run, now)
    gathered = deps.gather(deps.facts_deps, now)
    facts = as_holder(gathered) if dry_run else gathered
    actions = deps.plan(facts, now)
    results = deps.perform(actions, deps.exec_deps, deps.current_epoch, dry_run)
    try:
        return format_status(facts, actions, results, now)
    except Exception as exc:  # the actions already ran; report them rather than drop them
        return error_line(exc, now, results)


def _attempt(deps: RunDeps, dry_run: bool) -> None:
    """One guarded tick and its write; nothing raised here leaves the loop."""
    now = deps.now()
    try:
        line = tick(deps, dry_run, now)
    except Exception as exc:  # one bad tick must not stop the loop
        line = error_line(exc, now)
    _publish_status(deps, dry_run, line, now)
    try:
        write_status(line, deps.report_deps)
    except Exception as exc:  # a failed notify or echo must not stop the loop either
        # A broken echo leaves nowhere to write, so the loop goes on silent rather than dying.
        with contextlib.suppress(Exception):
            deps.report_deps.echo(error_line(exc, now))


def run(once: bool, interval: float, dry_run: bool, deps: RunDeps) -> None:
    """Ticks until interrupted or SIGTERM, or once; an interrupt stops the land worker, then releases the lease if held."""
    restore = (lambda: None) if dry_run else deps.install_sigterm()
    try:
        while True:
            _attempt(deps, dry_run)
            if once:
                return
            deps.sleep(interval)
    except KeyboardInterrupt:
        restore()  # a second SIGTERM during the release below ends the process as it would have
        with contextlib.suppress(Exception):  # a failed land release must not keep the chair lease held
            deps.stop_lands()
        if deps.holds():
            deps.release()
    finally:
        restore()
