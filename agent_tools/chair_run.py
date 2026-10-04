"""The chair tick loop: beat, gather, plan, perform, report and sleep, all through injected deps."""
from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from agent_tools import chair_exec, chair_report, usage_meter
from agent_tools.chair_exec import Result
from agent_tools.chair_facts import FactsDeps, gather_facts
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_report import EASTERN, format_status, write_status
from agent_tools.chair_types import Action, Facts, PlanTick

__all__ = ["DEFAULT_INTERVAL", "RunDeps", "as_holder", "error_line", "run", "tick"]

DEFAULT_INTERVAL = 60.0

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
    try:
        write_status(line, deps.report_deps)
    except Exception as exc:  # a failed notify or echo must not stop the loop either
        # A broken echo leaves nowhere to write, so the loop goes on silent rather than dying.
        with contextlib.suppress(Exception):
            deps.report_deps.echo(error_line(exc, now))


def run(once: bool, interval: float, dry_run: bool, deps: RunDeps) -> None:
    """Ticks until interrupted, or once; a KeyboardInterrupt releases the lease only if this process holds it."""
    try:
        while True:
            _attempt(deps, dry_run)
            if once:
                return
            deps.sleep(interval)
    except KeyboardInterrupt:
        if deps.holds():
            deps.release()
