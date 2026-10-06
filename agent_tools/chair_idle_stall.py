"""Pure rule for an idle-lanes stall: when it holds, and which blocker to name first."""

from datetime import UTC, datetime
from typing import Literal, TypedDict

from agent_tools.chair_types import IdleStallInputs

IDLE_STALL_MINUTES = 15
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class Diagnosis(TypedDict):
    kind: Literal["host", "stub", "land_ci", "blocked_needs", "unknown"]
    subject: str
    text: str
    signature: str  # kind:subject, so it moves with the first blocker and not with the counts


def minutes_between(earlier_ts: str | None, now: datetime) -> float | None:
    """Minutes from an ISO UTC timestamp to now; None when absent or unparseable."""
    if earlier_ts is None:
        return None
    try:
        earlier = datetime.strptime(earlier_ts, _TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None
    return (now - earlier).total_seconds() / 60


def _at_least(age: float | None, minutes: int) -> bool:
    return age is not None and age >= minutes


def is_stalled(inputs: IdleStallInputs, now: datetime) -> bool:
    """Exactly stall_minutes counts as stalled; an unknown age never does."""
    minutes = inputs["stall_minutes"]
    idle = (
        inputs["free_lanes"] > 0
        and inputs["ready"] + inputs["queued"] > 0
        and _at_least(minutes_between(inputs["last_progress_at"], now), minutes)
    )
    return idle or any(
        _at_least(minutes_between(land["waiting_since"], now), minutes) for land in inputs["lands_waiting"]
    )


def _diagnosis(
    kind: Literal["host", "stub", "land_ci", "blocked_needs", "unknown"], subject: str, text: str
) -> Diagnosis:
    return {"kind": kind, "subject": subject, "text": text, "signature": f"{kind}:{subject}"}


def diagnose(inputs: IdleStallInputs) -> Diagnosis:
    """First match wins: failed host, empty stub, land without checks, blocked ready task, unknown."""
    failed = [h for h in inputs["hosts"] if not h["ok"]]
    unchecked = [w for w in inputs["lands_waiting"] if not w["checks_started"]]
    if failed:
        host = failed[0]
        return _diagnosis("host", host["host"], f"host {host['host']} failed its check: {host['detail']}")
    if inputs["empty_stubs"]:
        stub = inputs["empty_stubs"][0]
        return _diagnosis("stub", stub, f"the intake has an initiative stub and no tasks: {stub}")
    if unchecked:
        land = unchecked[0]
        status = land["forge_status"]
        quoted = "" if status is None else f", forge status: {status!r}"
        return _diagnosis(
            "land_ci", land["run"], f"run {land['run']} pr {land['pr']} has not started its checks{quoted}"
        )
    if inputs["blocked_ready"]:
        entry = inputs["blocked_ready"][0]
        needs = ", ".join(entry["unlanded_needs"])
        return _diagnosis("blocked_needs", entry["task"], f"task {entry['task']} waits on unlanded needs: {needs}")
    counts = (
        f"free_lanes={inputs['free_lanes']} ready={inputs['ready']} "
        f"queued={inputs['queued']} lands_waiting={len(inputs['lands_waiting'])}"
    )
    return _diagnosis("unknown", "chair", f"unknown {counts}")
