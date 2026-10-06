"""The chair tick loop: beat, gather, plan, perform, report and sleep, all through injected deps."""
from __future__ import annotations

import contextlib
import json
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent_tools import (
    chair_exec,
    chair_land,
    chair_report,
    chair_tick_status,
    dash_chair_action,
    land_repo_lease,
    usage_meter,
)
from agent_tools.chair_beat import beat_in_thread
from agent_tools.chair_exec import Result
from agent_tools.chair_facts import FactsDeps, gather_facts
from agent_tools.chair_plan import TickPlan, plan_tick, plan_tick_held
from agent_tools.chair_report import EASTERN, format_status, write_status
from agent_tools.chair_types import Action, Facts, PlanTick
from agent_tools.ci_gate import DEFAULT_QUEUED_BOUND_SECONDS, CiGate, evaluate_gate
from agent_tools.forge_status import StatusReader

__all__ = [
    "DEFAULT_INTERVAL", "LAND_BEAT_INTERVAL", "GateState", "LeaseRejected", "RunDeps", "WorkerLands", "as_holder", "error_line", "finished_land",
    "land_sink", "next_gate_record", "no_landing", "no_lands_to_stop", "run", "tick",
]

DEFAULT_INTERVAL = 60.0
LAND_BEAT_INTERVAL = 30.0  # half a minute, so the chair lease is beaten at least once a minute while a land runs

Gather = Callable[[FactsDeps, datetime], Facts]
Perform = Callable[[list[Action], chair_exec.Deps, Callable[[], int], bool], list[Result]]
PlanHeld = Callable[[Facts, datetime, CiGate], TickPlan]


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


def no_landing() -> list[dict[str, str]]:
    """The landing source of a chair with no land worker."""
    return []


def no_gate_change(_gate: CiGate) -> None:
    """The set_gate hook of a chair with no land worker."""


def no_queued_lands() -> tuple[float, ...]:
    """The queued-since source of a chair with no land worker."""
    return ()


def sigterm_as_interrupt() -> Callable[[], None]:
    """Edge. SIGTERM raises KeyboardInterrupt in the main thread; the callable returned puts the old handler back."""

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, interrupt)
    except ValueError:  # signal handlers can only be set from the main thread
        return lambda: None
    return lambda: signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


@dataclass
class GateState:
    """What the CI gate was when the last acting tick finished; the one thing a tick carries to the next."""

    paused: bool = False


@dataclass(frozen=True)
class RunDeps:
    """`beat` renews the store lease; a lost renewal reaches the tick through the lease that `gather` reads next.

    `status_reader` is None for a chair with no CI gate; its fetch and clock are injected where it is built.
    `queued_since` lists epoch seconds, one per land waiting on checks; `set_gate` hands the verdict to the land worker.
    """

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
    runs_dir: Path | None = None  # None writes no tick status to the lease
    read_action: Callable[[Path], dict | None] = dash_chair_action.read_current_action
    write_tick_status: Callable[[Path, int, str], bool] = chair_tick_status.write_tick_status
    status_reader: StatusReader | None = None
    queued_since: Callable[[], tuple[float, ...]] = no_queued_lands
    set_gate: Callable[[CiGate], object] = no_gate_change
    plan_held: PlanHeld = plan_tick_held
    gate_state: GateState = field(default_factory=GateState)
    # The lease-only renewal run from the loop's own thread; it returns a refusal line or "" and touches no tick state. None starts no thread.
    lease_beat: Callable[[], object] | None = None
    lease_beat_interval: float = LAND_BEAT_INTERVAL
    # Receives the ids of the rows a live tick changed, exports the board and commits it; returns a note for the tick line, "" for none.
    export_rows: Callable[[tuple[str, ...]], str] | None = None


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


# A result changed a row when it is done, landed or recorded; the rows are its action's task_id, stale_tasks and intake_ids.
CHANGED_STATUSES = frozenset({"done", "landed", "recorded"})


def changed_row_ids(results: Sequence[Result]) -> tuple[str, ...]:
    """The distinct ids of the rows the results changed, in first-seen order."""
    actions = [r["action"] for r in results if r["status"] in CHANGED_STATUSES]
    ids = (row_id for a in actions for row_id in [a.get("task_id", ""), *a.get("stale_tasks", []), *a.get("intake_ids", [])])
    return tuple(dict.fromkeys(row_id for row_id in ids if row_id))


def _export_note(export: Callable[[tuple[str, ...]], str], ids: tuple[str, ...]) -> str:
    """Edge. The hook's note, or its failure as a line; a raising hook never leaves the tick."""
    try:
        return export(ids)
    except Exception as exc:  # the rows are already changed; the failure is reported, not raised
        return f"export failed: {type(exc).__name__}: {exc}"


class WorkerLands:
    """Edge. Lands handed to a LandWorker, one handle per repository, held until the next tick collects them.

    A land waits on checks from the moment its work starts until it returns, so `queued_since` lists the start
    times, in epoch seconds, of the lands running now. A land held by a paused gate has not started and has none.
    """

    def __init__(self, worker: chair_land.LandWorker, stamp: Callable[[], float] = time.time) -> None:
        self._worker = worker
        self._stamp = stamp
        self._handles: dict[str, tuple[Action, chair_land.LandHandle[Result]]] = {}
        self._gate = chair_land.UNPAUSED
        self._since: dict[str, float] = {}
        self._lock = threading.Lock()

    def set_gate(self, gate: CiGate) -> None:
        """Holds later lands while `gate` is paused; an unpaused gate starts the lands it held."""
        self._gate = gate
        self._worker.tick(gate)

    def queued_since(self) -> tuple[float, ...]:
        with self._lock:
            return tuple(self._since.values())

    def landing(self) -> list[dict[str, str]]:
        """One dict per land still in progress, read from the action it was submitted with."""
        return [
            {"initiative": a.get("initiative", ""), "phase": a.get("phase", ""), "repo": a.get("repo", "")}
            for a, h in list(self._handles.values())
            if h.in_progress()
        ]

    def _timed(self, repo: str, work: Callable[[], Result]) -> Callable[[], Result]:
        def timed() -> Result:
            with self._lock:
                self._since[repo] = self._stamp()
            try:
                return work()
            finally:
                with self._lock:
                    self._since.pop(repo, None)

        return timed

    def pending(self, repo: str) -> bool:
        return repo in self._handles

    def submit(self, action: Action, work: Callable[[], Result]) -> Result:
        repo = action.get("repo", "")
        submitted = self._worker.submit(repo, self._timed(repo, work), self._gate)
        if submitted.handle is None:
            holder = submitted.outcome.holder if isinstance(submitted.outcome, chair_land.Refused) else None
            reason = land_repo_lease.refusal_message(repo, holder or "another land")
            return {"action": action, "status": "busy", "reason": reason}
        self._handles[repo] = (action, submitted.handle)
        return {"action": action, "status": "in_progress", "reason": f"landing in {repo}"}

    def stop(self) -> None:
        """Refuses the queued lands and any later one; a land still running is left to finish."""
        self._worker.stop()

    def collect(self) -> list[Result]:
        done = [repo for repo, (_, handle) in self._handles.items() if not handle.in_progress()]
        return [finished_land(action, handle.result()) for action, handle in (self._handles.pop(repo) for repo in done)]


def land_sink(
    beat: Callable[[], None], clock: Callable[[], float] = time.monotonic, wait: Callable[[float], None] = time.sleep,
    stamp: Callable[[], float] = time.time,
) -> WorkerLands:
    """Edge. The worker beats the chair lease through `beat` every LAND_BEAT_INTERVAL while any land runs."""
    return WorkerLands(chair_land.LandWorker(beat, LAND_BEAT_INTERVAL, clock, wait), stamp)


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


def _publish_tick_status(deps: RunDeps, dry_run: bool, line: str, now: datetime) -> None:
    """Write the tick's status JSON to the chair lease row under the held epoch; a False return is not an error."""
    if dry_run or not line.strip() or deps.runs_dir is None:
        return
    with contextlib.suppress(Exception):
        action = deps.read_action(deps.runs_dir)
        text = chair_tick_status.dump_tick_status(chair_tick_status.build_tick_status(line, now, action))
        deps.write_tick_status(deps.runs_dir, deps.current_epoch(), text)


def next_gate_record(was_paused: bool, gate: CiGate, held: tuple[str, ...], epoch: int) -> Action | None:
    """The one needs_chair for a gate that has just paused; the cause carries the reason, as perform records no reason."""
    if was_paused or not gate.paused:
        return None
    named = f"; held for CI: {', '.join(held)}" if held else ""
    return {"kind": "needs_chair", "initiative": "ci-gate", "cause": f"ci_gate_paused: {gate.reason}{named}", "epoch": epoch}


def _evaluate_gate(deps: RunDeps, now: datetime) -> CiGate:
    if deps.status_reader is None:
        return chair_land.UNPAUSED
    return evaluate_gate(deps.status_reader.read(), deps.queued_since(), now.timestamp(), DEFAULT_QUEUED_BOUND_SECONDS)


def tick(deps: RunDeps, dry_run: bool, now: datetime) -> str:
    """Beat first, then gather, plan, perform and format; a failure after perform still names what was performed.

    A dry run never takes the lease, so it plans as the holder would to show the actions a live tick would take.
    The meter is published right after the beat, so a gather, plan or perform that raises cannot skip it.
    A paused CI gate holds lands and dependent relaunches and records one needs_chair when it first pauses. The pause
    is remembered after perform, and only by a tick that acts as the holder, so a tick that fails records it again.
    A live tick that changed rows hands their ids to `export_rows`; its note or failure is appended to the line.
    """
    deps.beat()
    _publish_meter(deps, dry_run, now)
    gathered = deps.gather(deps.facts_deps, now)
    facts = as_holder(gathered) if dry_run else gathered
    gate = _evaluate_gate(deps, now)
    if not dry_run:
        deps.set_gate(gate)
    planned = deps.plan_held(facts, now, gate) if gate.paused else TickPlan(deps.plan(facts, now), ())
    acting = not dry_run and all(a.get("kind") != "standby" for a in planned.actions)
    notice = next_gate_record(deps.gate_state.paused, gate, planned.held_for_ci, deps.current_epoch()) if acting and gate.paused else None
    actions = [*planned.actions, notice] if notice is not None else planned.actions
    results = deps.perform(actions, deps.exec_deps, deps.current_epoch, dry_run)
    if acting and gate.paused != deps.gate_state.paused:
        deps.gate_state.paused = gate.paused
    ids = () if dry_run or deps.export_rows is None else changed_row_ids(results)
    note = _export_note(deps.export_rows, ids) if ids and deps.export_rows is not None else ""
    try:
        line = format_status(facts, actions, results, now)
    except Exception as exc:  # the actions already ran; report them rather than drop them
        line = error_line(exc, now, results)
    return f"{line} | {note}" if note else line


def _attempt(deps: RunDeps, dry_run: bool) -> None:
    """One guarded tick and its write; nothing raised here leaves the loop."""
    now = deps.now()
    try:
        line = tick(deps, dry_run, now)
    except Exception as exc:  # one bad tick must not stop the loop
        line = error_line(exc, now)
    _publish_status(deps, dry_run, line, now)
    _publish_tick_status(deps, dry_run, line, now)
    try:
        write_status(line, deps.report_deps)
    except Exception as exc:  # a failed notify or echo must not stop the loop either
        # A broken echo leaves nowhere to write, so the loop goes on silent rather than dying.
        with contextlib.suppress(Exception):
            deps.report_deps.echo(error_line(exc, now))


class LeaseRejected(Exception):
    """The lease code refused a beat: a newer epoch or another holder owns the lease."""


def _fenced(beat: Callable[[], object]) -> Callable[[], None]:
    """A refusal line from the lease code becomes a raise, so the beat thread stops on it."""

    def call() -> None:
        refusal = beat()
        if refusal:
            raise LeaseRejected(str(refusal))

    return call


def start_lease_beat(deps: RunDeps, dry_run: bool) -> threading.Event:
    """Edge. Starts the daemon beat thread unless this is a dry run or no beat is wired, and returns its stop event.

    A rejected or raising beat sets the event, so the thread stops; `gather` then reads the lost lease on the next tick.
    """
    stop = threading.Event()
    if deps.lease_beat is not None and not dry_run:
        beat_in_thread(_fenced(deps.lease_beat), deps.lease_beat_interval, stop, lambda _exc: stop.set())
    return stop


def run(once: bool, interval: float, dry_run: bool, deps: RunDeps) -> None:
    """Ticks until interrupted or SIGTERM, or once; an interrupt stops the land worker, then releases the lease if held."""
    restore = (lambda: None) if dry_run else deps.install_sigterm()
    beating = start_lease_beat(deps, dry_run)
    try:
        while True:
            _attempt(deps, dry_run)
            if once:
                return
            deps.sleep(interval)
    except KeyboardInterrupt:
        beating.set()  # before the release: a beat with no sidecar acquires, and would retake the lease just released
        restore()  # a second SIGTERM during the release below ends the process as it would have
        with contextlib.suppress(Exception):  # a failed land release must not keep the chair lease held
            deps.stop_lands()
        if deps.holds():
            deps.release()
    finally:
        beating.set()
        restore()
