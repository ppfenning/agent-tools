"""Pure detection of push events from one chair tick: the gathered facts and the planned actions in, events out."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agent_tools.chair_facts import STRANDED_CAUSE
from agent_tools.notify_core import Event

HARNESS_FAILURE_LIMIT = 2


def _host_login(action: Mapping) -> Event:
    host = str(action.get("host") or "")
    url = str(action.get("url") or "")
    body = (
        f"The claude login on {host} has lapsed. Open the login link."
        if url
        else f"The claude login on {host} has lapsed. Run the claude login on that host."
    )
    return Event("host_login", f"host_login:{host}", f"Login lapsed on {host}", body, url)


def _stall(action: Mapping) -> Event:
    initiative = str(action.get("initiative") or "")
    reason = str(action.get("reason") or "")
    if action.get("cause") == "stalled":
        run = str(action.get("run") or "")
        return Event("stall", f"stall:{run}", f"Run stalled: {initiative}", f"Run {run} of {initiative}: {reason}")
    return Event(
        "stall",
        f"stall:idle:{action.get('signature') or initiative}",
        f"Idle stall: {initiative}",
        reason,
    )


def _failures(action: Mapping, facts: Mapping) -> int:
    """The action's own count when it has one, else the highest count on a quarantine row of the same initiative."""
    own = action.get("harness_failures")
    return (
        int(own)
        if own is not None
        else max(
            (
                int(row.get("harness_failures") or 0)
                for row in facts.get("quarantines") or []
                if row.get("initiative") == action.get("initiative")
            ),
            default=0,
        )
    )


def _needs_chair(action: Mapping, facts: Mapping) -> Event | None:
    # No action carries a person field, so the cause rule stands in: stranded, or repeated harness failures.
    cause = str(action.get("cause") or "")
    if cause != STRANDED_CAUSE and _failures(action, facts) < HARNESS_FAILURE_LIMIT:
        return None
    initiative = str(action.get("initiative") or "")
    reason = str(action.get("reason") or "")
    body = f"{initiative} needs you: {cause}." + (f" {reason}" if reason else "")
    return Event("needs_chair", f"needs_chair:{initiative}:{cause}", f"Needs you: {initiative}", body)


def _event(action: Mapping, facts: Mapping) -> Event | None:
    cause = action.get("cause")
    if cause == "login_lapsed":
        return _host_login(action)
    if cause in ("stalled", "idle_stall"):
        return _stall(action)
    return _needs_chair(action, facts)


def events_from_tick(facts: Mapping, actions: Sequence[Mapping]) -> list[Event]:
    """Events in action order, one per key. Only needs_chair actions are read."""
    found = [
        e
        for a in actions
        if a.get("kind") == "needs_chair"
        for e in [_event(a, facts)]
        if e is not None
    ]
    return [e for i, e in enumerate(found) if e.key not in {p.key for p in found[:i]}]
