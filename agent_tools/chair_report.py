"""The per-tick status line of the chair loop: a pure formatter and a thin writer.

`format_status` reads only its arguments, the time included. `write_status` prints and, when a
notify callable is injected, sends the same line. Nothing here performs an action or gathers a fact.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from agent_tools.chair_exec import Result
from agent_tools.chair_types import Action, Facts
from agent_tools.notify import Notification

__all__ = ["Deps", "format_status", "lands_this_tick", "mode_of", "needs_chair_items", "write_status"]

EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Deps:
    echo: Callable[[str], None]
    notify: Callable[[Notification], None] | None = None  # absent means print only


def lands_this_tick(results: Sequence[Result]) -> int:
    """chair_exec marks a land `landed` only when it exited 0 and reported both merge and mark_done."""
    return sum(1 for r in results if r["action"].get("kind") == "land" and r["status"] == "landed")


def mode_of(actions: Sequence[Action], results: Sequence[Result]) -> str:
    """dry-run when every result is a dry_run, else standby with its holder and host, else holding."""
    standby = next((a for a in actions if a.get("kind") == "standby"), None)
    if results and all(r["status"] == "dry_run" for r in results):
        return "dry-run"
    if standby is not None:
        return f"standby holder={standby.get('holder', '?')} host={standby.get('host', '?')}"
    return "holding"


def needs_chair_items(actions: Sequence[Action]) -> list[str]:
    return [f"{a.get('initiative', '?')}:{a.get('cause', '?')}" for a in actions if a.get("kind") == "needs_chair"]


def _five_hour(limits: dict) -> str:
    """LimitsFacts carries no 5-hour field yet; show one when the edge supplies `five_hour_fraction`."""
    fraction = limits.get("five_hour_fraction")
    return "5h n/a" if fraction is None else f"5h {fraction:.0%}"


def format_status(facts: Facts, actions: Sequence[Action], results: Sequence[Result], now: datetime) -> str:
    """One line per tick. `now` must be timezone-aware; it is printed in Eastern time."""
    limits, dispatch = facts["limits"], facts["dispatch"]
    stop = " hard stop" if limits["hard_stop"] else ""
    needs = needs_chair_items([*actions, *(r["action"] for r in results if r["status"] == "escalated")])
    parts = [
        f"chair {now.astimezone(EASTERN):%m-%d %H:%M %Z}",
        f"lanes {dispatch['live_runs']}/{dispatch['max_in_flight']}",
        f"lands {lands_this_tick(results)}",
        f"limits {_five_hour(dict(limits))} weekly {limits['weekly_fraction']:.0%}/{limits['hard_stop_fraction']:.0%}{stop}",
        mode_of(actions, results),
        f"needs chair: {', '.join(needs)}" if needs else "needs chair: none",
    ]
    return " | ".join(parts)


def write_status(line: str, deps: Deps) -> None:
    """Edge. Print the line; send it too only when a notify callable was injected."""
    deps.echo(line)
    if deps.notify is not None:
        deps.notify(Notification("chair tick", line, "low"))
