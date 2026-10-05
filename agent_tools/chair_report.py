"""The per-tick status line of the chair loop: a pure formatter and a thin writer.

`format_status` reads only its arguments, the time included. `write_status` prints and, when a
notify callable is injected, sends the same line. Nothing here performs an action or gathers a fact.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from agent_tools.chair_exec import LAUNCH_KINDS, Result
from agent_tools.chair_read_quarantined import RUNAWAY_CAUSE
from agent_tools.chair_types import EASTERN, Action, Facts, ReviewPr
from agent_tools.notify import Notification

__all__ = [
    "EASTERN", "Deps", "echo_line", "failed_items", "fetched_items", "format_status", "housekeeping_fragment",
    "landing_items", "lands_this_tick", "launched_items", "mode_of", "needs_chair_items", "would_items", "write_status",
]


@dataclass(frozen=True)
class Deps:
    echo: Callable[[str], None]
    notify: Callable[[Notification], None] | None = None  # absent means print only


def lands_this_tick(results: Sequence[Result]) -> int:
    """chair_exec marks a land or land_phase `landed` only when it exited 0 and reported both merge and mark_done."""
    return sum(1 for r in results if r["action"].get("kind") in ("land", "land_phase") and r["status"] == "landed")


def mode_of(actions: Sequence[Action], results: Sequence[Result]) -> str:
    """dry-run when every result is a dry_run, else standby with its holder and host, else holding."""
    standby = next((a for a in actions if a.get("kind") == "standby"), None)
    if results and all(r["status"] == "dry_run" for r in results):
        return "dry-run"
    if standby is not None and standby.get("until"):
        return f"standby: held by {standby.get('holder', '?').partition('@')[0]} until {standby['until']}"
    if standby is not None:
        return f"standby holder={standby.get('holder', '?')} host={standby.get('host', '?')}"
    return "holding"


def _scope(action: Action) -> str:
    """The initiative, as `<initiative>/<phase>` when the action names a phase: a land_phase carries no task_id."""
    initiative, phase = action.get("initiative", "?"), action.get("phase", "")
    return f"{initiative}/{phase}" if phase else initiative


def _target(action: Action) -> str:
    return action.get("task_id") or _scope(action)


def needs_chair_items(actions: Sequence[Action]) -> list[str]:
    """Runaway-ceiling entries first, each bare (`RUNAWAY <scope>`); the rest keep `needs chair: <scope>:<cause>`."""
    chair_actions = [a for a in actions if a.get("kind") == "needs_chair"]
    runaway = [a for a in chair_actions if a.get("cause") == RUNAWAY_CAUSE]
    rest = [a for a in chair_actions if a.get("cause") != RUNAWAY_CAUSE]
    return [f"RUNAWAY {_scope(a)}" for a in runaway] + [
        f"needs chair: {_scope(a)}:{a.get('cause', '?')}" for a in rest
    ]


def _launched_item(action: Action) -> str:
    target = action.get("initiative") or action.get("task_id", "?")
    host = action.get("host")
    if host and action.get("kind") in ("relaunch", "retry", "launch_epic"):  # the kinds `argv_for` sends `--on` for
        return f"epic:{target}@{host}"
    return f"{action.get('kind')}:{target}"


def launched_items(results: Sequence[Result]) -> list[str]:
    """`<kind>:<initiative>` for every launch, relaunch, retry or rescue that ran; an epic launch on a lane host is `epic:<initiative>@<host>`."""
    return [
        _launched_item(r["action"])
        for r in results
        if r["status"] == "done" and r["action"].get("kind") in LAUNCH_KINDS
    ]


def failed_items(results: Sequence[Result]) -> list[str]:
    """`<kind>:<task or initiative>` for every refused, failed or not_landed result; a land_phase is `<initiative>/<phase>`."""
    return [
        f"{r['action'].get('kind')}:{_target(r['action'])}"
        for r in results
        if r["status"] in ("refused", "failed", "not_landed")
    ]


def landing_items(results: Sequence[Result]) -> list[str]:
    """`<task or phase> in <repo>` for every land still running behind the tick."""
    return [
        f"{r['action'].get('task_id') or r['action'].get('phase', '')} in {r['action'].get('repo', '')}"
        for r in results
        if r["status"] == "in_progress"
    ]


def fetched_items(results: Sequence[Result]) -> list[str]:
    """`<run> from <host>` for every fetch_exit that landed; a host of None reads `on another machine`."""
    return [
        f"{r['action'].get('run', '?')} from {'on another machine' if r['action'].get('host') is None else r['action'].get('host')}"
        for r in results
        if r["status"] == "done" and r["action"].get("kind") == "fetch_exit"
    ]


def would_items(results: Sequence[Result]) -> list[str]:
    """`<kind>:<task or initiative>` for every dry_run result except standby and take_lease; a land_phase is `<initiative>/<phase>`."""
    return [
        f"{r['action'].get('kind')}:{_target(r['action'])}"
        for r in results
        if r["status"] == "dry_run" and r["action"].get("kind") not in ("standby", "take_lease")
    ]


def _five_hour(limits: dict) -> str:
    """`5h <fraction> (<source>)`; `5h n/a` when the edge supplies no `five_hour_fraction`."""
    fraction = limits.get("five_hour_fraction")
    if fraction is None:
        return "5h n/a"
    return f"5h {fraction:.0%} ({limits.get('window_source', 'est')})"


def _weekly(limits: dict) -> str:
    """`weekly <fraction>/<hard stop fraction> (<source>)`, with `since <window_start_day>` when the edge supplies one."""
    base = f"weekly {limits['weekly_fraction']:.0%}/{limits['hard_stop_fraction']:.0%} ({limits.get('weekly_source', 'est')})"
    window_start_day = limits.get("window_start_day")
    return base if window_start_day is None else f"{base} since {window_start_day}"


def housekeeping_fragment(last_housekeeping_at: str | None, now: datetime) -> str:
    """`housekeeping <age>`, minutes under an hour, hours under 48, else days; `never` when unset, unparseable or naive."""
    try:
        then = datetime.fromisoformat(last_housekeeping_at) if last_housekeeping_at else None
    except ValueError:
        return "housekeeping never"
    if then is None or then.tzinfo is None:  # a naive stamp cannot be subtracted from the tz-aware `now`
        return "housekeeping never"
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 60:
        return f"housekeeping {minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"housekeeping {hours}h"
    return f"housekeeping {hours // 24}d"


def review_fragment(review_prs: Sequence[ReviewPr]) -> str:
    """`review: N awaiting (task url, ...)`; empty when none are awaiting."""
    items = [pr["task_id"] + " " + pr["url"] for pr in review_prs]
    return f"review: {len(items)} awaiting ({', '.join(items)})" if items else ""


def format_status(facts: Facts, actions: Sequence[Action], results: Sequence[Result], now: datetime) -> str:
    """One line per tick. `now` must be timezone-aware; it is printed in Eastern time."""
    limits, dispatch = facts["limits"], facts["dispatch"]
    stop = " hard stop" if limits["hard_stop"] else ""
    needs = needs_chair_items([*actions, *(r["action"] for r in results if r["status"] == "escalated")])
    launched, fetched, failed, would, landing = (
        launched_items(results), fetched_items(results), failed_items(results), would_items(results),
        landing_items(results),
    )
    drafts = facts.get("drafts", 0)
    review = review_fragment(facts.get("review_prs", []))
    parts = [
        f"chair {now.astimezone(EASTERN):%m-%d %H:%M %Z}",
        f"lanes {dispatch['live_runs']}/{dispatch['max_in_flight']}",
        f"lands {lands_this_tick(results)}",
        *([f"drafts {drafts}"] if drafts > 0 else []),
        f"limits {_five_hour(dict(limits))} {_weekly(dict(limits))}{stop}",
        mode_of(actions, results),
        *([f"would: {', '.join(would)}"] if would else []),
        *([f"landing: {', '.join(landing)}"] if landing else []),
        *([f"launched: {', '.join(launched)}"] if launched else []),
        *([f"fetched: {', '.join(fetched)}"] if fetched else []),
        *([f"failed: {', '.join(failed)}"] if failed else []),
        *([review] if review else []),
        *(f"skipped missing repo {p}" for p in facts.get("missing_repos", [])),
        ", ".join(needs) if needs else "needs chair: none",
        *([housekeeping_fragment(facts["last_housekeeping_at"], now)] if "last_housekeeping_at" in facts else []),
    ]
    return " | ".join(parts)


def echo_line(line: str) -> None:
    """Edge. Flushes, because stdout redirected to a file is block-buffered and a service log would stay empty."""
    print(line, flush=True)


def write_status(line: str, deps: Deps) -> None:
    """Edge. Print the line; send it too only when a notify callable was injected."""
    deps.echo(line)
    if deps.notify is not None:
        deps.notify(Notification("chair tick", line, "low"))
