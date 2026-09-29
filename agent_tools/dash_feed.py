"""Pure assembly of the versioned dash feed snapshot shape, plus the edge that
gathers a live one from the fleet's existing readers: `console_screen.gather`
for chair/spend/machines/runs, `run_store.read_queue` for the initiative
queue, and `courier.inbox` for the unacknowledged bus.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path

from agent_tools import console_screen, courier, route, run_store, usage_meter, usage_window

__all__ = ["gather_feed", "snapshot"]

_LANDED_STATES = ("done", "dropped")
_DEFAULT_MAX_IN_FLIGHT = 3
_DEFAULT_PROFILE_PATH = Path.home() / ".config" / "agent-tools" / "profile.yaml"


def snapshot(at, chair, spend, machines, runs, queue, inbox, watch) -> dict:
    """Assemble the schema-1 dash feed snapshot from its sections."""
    return {
        "schema": 1,
        "at": at,
        "chair": chair,
        "spend": spend,
        "machines": machines,
        "runs": runs,
        "queue": queue,
        "inbox": inbox,
        "watch": watch,
    }


def _parse_now(now: str) -> datetime:
    return datetime.fromisoformat(str(now).replace("Z", "+00:00"))


def _fraction(spent: float, ceiling: float | None) -> float | None:
    return None if not ceiling or ceiling <= 0 else spent / ceiling


def _profile_path() -> Path:
    """`$AGENT_TOOLS_PROFILE` when set, else `~/.config/agent-tools/profile.yaml`."""
    override = os.environ.get("AGENT_TOOLS_PROFILE")
    return Path(override).expanduser() if override else _DEFAULT_PROFILE_PATH


def _profile_ceilings() -> tuple[float | None, float | None]:
    """`(window_ceiling_usd, weekly_ceiling_usd)` from the routing profile; `(None, None)` when the file is
    missing, unreadable, or fails to parse."""
    try:
        text = _profile_path().read_text(encoding="utf-8")
    except OSError:
        return None, None
    try:
        profile = route.parse_profile(text)
    except route.ProfileError:
        return None, None
    return profile.get("window_ceiling_usd"), profile.get("weekly_ceiling_usd")


def _spend(runs_dir: Path, now: str) -> dict:
    """The spend header: a fresh usage meter wins outright; otherwise an estimate ceilinged by an implied
    ceiling the meter has recorded, falling back to the routing profile's own ceilings."""
    at = _parse_now(now)
    meter = usage_meter.read()
    if meter is not None and usage_meter.fresh(meter, at):
        return {
            "five_hour_fraction": meter.five_hour.used_percentage / 100,
            "five_hour_source": "meter",
            "five_hour_resets_at": meter.five_hour.resets_at.isoformat(),
            "weekly_fraction": meter.seven_day.used_percentage / 100,
            "weekly_source": "meter",
            "weekly_resets_at": meter.seven_day.resets_at.isoformat(),
            "hard_stop_fraction": usage_window.DEFAULT_POLICY.weekly_hard_stop_fraction,
        }
    profile_window_ceiling, profile_weekly_ceiling = _profile_ceilings()
    window_ceiling = usage_meter.implied_ceiling("five_hour", at)
    if window_ceiling is None:
        window_ceiling = profile_window_ceiling
    weekly_ceiling = usage_meter.implied_ceiling("weekly", at)
    if weekly_ceiling is None:
        weekly_ceiling = profile_weekly_ceiling
    window = usage_window.gather(runs_dir, at, ceiling_usd=window_ceiling)
    weekly = usage_window.gather_weekly(runs_dir, at, weekly_ceiling_usd=weekly_ceiling)
    return {
        "five_hour_fraction": _fraction(window.spent_usd, window.ceiling_usd),
        "five_hour_source": "est",
        "five_hour_resets_at": None,
        "weekly_fraction": _fraction(weekly.spent_usd, weekly.ceiling_usd),
        "weekly_source": "est",
        "weekly_resets_at": None,
        "hard_stop_fraction": usage_window.DEFAULT_POLICY.weekly_hard_stop_fraction,
    }


def _local_identity(runs_dir: Path) -> tuple[str, int]:
    """This machine's own name and lane capacity: `socket.gethostname()` and `<runs_dir>/policy.pacing.json`'s
    `max_in_flight` when it is a positive int, else the same default the chair loop falls back to."""
    # Not the cap `chair run` enforces for every team: `cli._chair_max_in_flight` reads the team cartridge's
    # `policy.dispatch.max_in_flight` and its `extends` chain first. That chain is cli-private and not read here.
    try:
        raw = json.loads((Path(runs_dir) / "policy.pacing.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    value = raw.get("max_in_flight") if isinstance(raw, dict) else None
    capacity = value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else _DEFAULT_MAX_IN_FLIGHT
    return socket.gethostname(), capacity


def _initiative_progress(rows: list[dict], initiative: str) -> tuple[int, int, str | None]:
    """`(phases_landed, phases_total, current_phase)` for one initiative's task rows: a phase is landed when
    every one of its items is `done` or `dropped`, the same rule `console_screen._phase_progress` uses.
    `current_phase` is the first not-yet-landed phase in sorted order, or the last phase once all have landed."""
    by_phase: dict[str, list[str]] = {}
    for row in rows:
        if row.get("kind") == "task" and row.get("initiative") == initiative:
            by_phase.setdefault(row.get("phase"), []).append(row.get("state"))
    phases = sorted(by_phase)
    landed_flags = {phase: bool(states) and all(s in _LANDED_STATES for s in states) for phase, states in by_phase.items()}
    landed = sum(1 for ok in landed_flags.values() if ok)
    current = next((phase for phase in phases if not landed_flags[phase]), phases[-1] if phases else None)
    return landed, len(phases), current


def _priority(rows: list[dict], initiative: str):
    """The first `priority` an initiative's own rows carry in `extra` (arbitrary frontmatter passed through
    by `queue_rows.parse_item`); `None` when no row names one."""
    return next(
        (
            row["extra"]["priority"]
            for row in rows
            if row.get("initiative") == initiative and isinstance(row.get("extra"), dict) and "priority" in row["extra"]
        ),
        None,
    )


def _queue(runs_dir: Path) -> list[dict]:
    """One row per initiative the store's queue names, not only ones with a live lane."""
    rows = run_store.read_queue(runs_dir)
    initiatives = sorted({row["initiative"] for row in rows if row.get("initiative")})
    queue = []
    for initiative in initiatives:
        landed, total, current = _initiative_progress(rows, initiative)
        queue.append({
            "initiative": initiative,
            "priority": _priority(rows, initiative),
            "phases_landed": landed,
            "phases_total": total,
            "current_phase": current,
        })
    return queue


def _inbox(work_dir: Path) -> list[dict]:
    """Every unacknowledged bus entry for every seat, read once. With no label `courier.inbox` never consults
    the lock holder, so the chair lock is not read."""
    try:
        blob = (Path(work_dir) / "courier.jsonl").read_text(encoding="utf-8")
    except OSError:
        blob = ""
    return courier.inbox(blob)


def gather_feed(runs_dir: Path, work_dir: Path, now: str) -> dict:
    """Edge: the live schema-1 snapshot, built from the fleet's existing readers and nothing re-derived."""
    runs_dir, work_dir = Path(runs_dir), Path(work_dir)
    local_name, local_capacity = _local_identity(runs_dir)
    spend = _spend(runs_dir, now)
    sections = console_screen.gather(runs_dir, work_dir, now, local_name, local_capacity, spend)
    chair_row = sections["chair"][0] if sections["chair"] else {}
    return snapshot(
        now,
        chair_row,
        sections["spend"],
        sections["hosts"],
        sections["lanes"],
        _queue(runs_dir),
        _inbox(work_dir),
        [],
    )
