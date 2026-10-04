"""Pure assembly of the versioned dash feed snapshot shape, plus the edge that
gathers a live one from the fleet's existing readers: `console_screen.gather`
for chair/spend/machines/runs, `run_store.read_queue` for the initiative
queue, and `courier.inbox` for the unacknowledged bus.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path

from agent_tools import (
    chair,
    chair_capacity,
    chair_login_check,
    console_screen,
    courier,
    decisions,
    route,
    run_store,
    usage_meter,
    usage_window,
)
from agent_tools.chair_read_record import ACTION_LOG
from agent_tools.chair_types import EASTERN
from agent_tools.dash_chair_action import read_current_action
from agent_tools.dash_chair_beat import beat_age_s, read_status_record, status_from_store
from agent_tools.dash_chair_today import chair_today
from agent_tools.runs_detail import NODE_ORDER

__all__ = ["gather_feed", "snapshot"]

_LANDED_STATES = ("done", "dropped")
_LOGIN_CHECK_TIMEOUT_S = 15
_DEFAULT_PROFILE_PATH = Path.home() / ".config" / "agent-tools" / "profile.yaml"
_MAX_ROWS = 50
_DECISION_PREFIX = "coxswain://decision/"
_ASK_KEYS = ("options", "context", "asked_at", "note")
# The graphs `cox route launch` starts (cli.py's route launch commands); courier `to` labels name graphs too.
_GRAPH_NAMES = ("epic", "decompose", "rescue", "cos", "sweep")
# Every seat that is neither the chair nor a person: each node in the graph roster, and each graph.
_SEATS = frozenset((*NODE_ORDER, *_GRAPH_NAMES))


def snapshot(at, chair, spend, machines, runs, queue, queue_total, inbox, inbox_total, watch, decisions=()) -> dict:
    """Assemble the schema-1 dash feed snapshot from its sections. `queue_total`/`inbox_total` are each
    section's filtered count before the 50-row cap, for coxtop to render "50 of N". `decisions` is the open
    decision asks, newest first."""
    # coxswain-dash carries its own copy of tests/fixtures/dash_feed_v1.json and will need these two fields too.
    return {
        "schema": 1,
        "at": at,
        "chair": chair,
        "spend": spend,
        "machines": machines,
        "runs": runs,
        "queue": queue,
        "queue_total": queue_total,
        "inbox": inbox,
        "inbox_total": inbox_total,
        "decisions": list(decisions),
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


def _hard_stop_fraction(runs_dir: Path) -> float:
    """The weekly hard stop the chair loop enforces, from the loader it uses on the same runs directory."""
    # Deferred: cli.py imports this module, so a top-level import of cli is circular.
    from agent_tools import cli

    return cli._resolved_pacing_policy(Path(runs_dir)).weekly_hard_stop_fraction


def _spend(runs_dir: Path, now: str) -> dict:
    """The spend header: a fresh usage meter wins outright; otherwise an estimate ceilinged by an implied
    ceiling the meter has recorded, falling back to the routing profile's own ceilings."""
    at = _parse_now(now)
    hard_stop_fraction = _hard_stop_fraction(runs_dir)
    meter = usage_meter.read()
    if meter is not None and usage_meter.fresh(meter, at):
        return {
            "five_hour_fraction": meter.five_hour.used_percentage / 100,
            "five_hour_source": "meter",
            "five_hour_resets_at": meter.five_hour.resets_at.isoformat(),
            "weekly_fraction": meter.seven_day.used_percentage / 100,
            "weekly_source": "meter",
            "weekly_resets_at": meter.seven_day.resets_at.isoformat(),
            "hard_stop_fraction": hard_stop_fraction,
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
        "hard_stop_fraction": hard_stop_fraction,
    }


def _local_identity(runs_dir: Path, profile: dict) -> tuple[str, int]:
    """This machine's own name and the lane capacity `cox chair run` enforces, from `chair_capacity`."""
    return socket.gethostname(), chair_capacity.chair_max_in_flight(Path(runs_dir), profile)


def _run_capturing(argv: list[str]) -> tuple[int, str]:
    """Edge. `(exit code, stdout and stderr joined)`; `(1, "")` when the command is missing or times out."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=_LOGIN_CHECK_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return done.returncode, done.stdout + done.stderr


def _local_login(local_name: str) -> bool | None:
    """Edge. Whether `claude auth status` says this machine is logged in, as `chair_login_check` reads it for
    a host named `socket.gethostname()`: True or False when the reply says so, None when it cannot be read."""
    return chair_login_check._login_ok(local_name, "", _run_capturing)


def _local_versions(login_ok: bool | None, now: str) -> dict | None:
    """The local row's `versions_json` after a login check at `now`; None when the check could not tell, so
    the row stays unchecked and reads as unknown."""
    return None if login_ok is None else {"login_ok": login_ok, "login_checked_at": now}


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


def _priority_key(priority) -> tuple[int, int]:
    """Sort key for a `priority` value: numeric values ascending (1 first), anything absent or non-numeric last."""
    try:
        return (0, int(priority))
    except (TypeError, ValueError):
        return (1, 0)


def _queue(runs_dir: Path) -> tuple[list[dict], int]:
    """Initiatives the store's queue names with at least one phase not landed (a fully landed initiative drops
    out entirely), ordered by priority, then by first appearance in `read_queue`'s row order. Capped at
    `_MAX_ROWS`; the second element is the filtered count before the cap."""
    # Not a verified age. Queue rows carry no timestamp (`run_store._QUEUE_COLUMNS`), and the
    # `harness.store_queue read` contract (run_store.py:1009) promises no row order. Row order is age only
    # if the harness returns rows in insertion order, and that is unknown from this repository.
    rows = run_store.read_queue(runs_dir)
    # Walked backwards, so the last write for each initiative is its first index.
    first_seen = {row["initiative"]: index for index, row in reversed(list(enumerate(rows))) if row.get("initiative")}
    progress = {initiative: _initiative_progress(rows, initiative) for initiative in first_seen}
    priorities = {initiative: _priority(rows, initiative) for initiative in first_seen}
    open_initiatives = sorted(
        (initiative for initiative in first_seen if progress[initiative][0] < progress[initiative][1]),
        key=lambda initiative: (_priority_key(priorities[initiative]), first_seen[initiative]),
    )
    queue = [
        {
            "initiative": initiative,
            "priority": priorities[initiative],
            "phases_landed": progress[initiative][0],
            "phases_total": progress[initiative][1],
            "current_phase": progress[initiative][2],
        }
        for initiative in open_initiatives
    ]
    return queue[:_MAX_ROWS], len(queue)


def _reaches_a_person(to: str) -> bool:
    """True for `chair` (or a `chair-*` label, docs/design/courier.md's Labels section) or a person; false for
    any node or graph in `_SEATS`. Courier keeps no person registry, so a label outside `_SEATS` reads as a person."""
    return to == "chair" or to.startswith("chair-") or to not in _SEATS


def _courier_blob(work_dir: Path) -> str:
    """The raw courier.jsonl; "" when it cannot be read."""
    try:
        return (Path(work_dir) / "courier.jsonl").read_text(encoding="utf-8")
    except OSError:
        return ""


def _inbox(entries: list[dict]) -> tuple[list[dict], int]:
    """The `entries` addressed to `chair` or to a person, newest first (any other seat drops out entirely).
    With no label `courier.inbox` never consults the lock holder, so the chair lock is not read. Capped at
    `_MAX_ROWS`; the second element is the filtered count before the cap."""
    # `courier.inbox` preserves each id's first-appearance (oldest-first) order; reverse for newest first.
    reached = [e for e in entries if _reaches_a_person(str(e.get("to") or ""))][::-1]
    return reached[:_MAX_ROWS], len(reached)


def _is_decision(entry: dict) -> bool:
    return str(entry.get("ref") or "").startswith(_DECISION_PREFIX)


def _open_decisions(entries: list[dict], blob: str) -> list[dict]:
    """Unacknowledged asks in `entries` with no answer anywhere in `blob`, newest first. An answer carries
    `answer`; an ask carries `_ASK_KEYS`, and a decision entry with neither is left out."""
    asks = [e for e in entries if _is_decision(e) and "answer" not in e and all(key in e for key in _ASK_KEYS)]
    # An acked answer drops out of `courier.inbox`, so answers come from every line of the blob, acked or not.
    lines = [json.loads(line) for line in blob.splitlines() if line.strip()]
    answers = [e for e in lines if _is_decision(e) and "answer" in e]
    return decisions.open_decisions_newest_first(decisions.merge_decisions(asks, answers))


def _age_s(text, at: datetime) -> int:
    """Whole seconds from an ISO `text` to `at`, never negative; 0 when `text` is missing or unreadable."""
    try:
        return max(0, int((at - _parse_now(text)).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _chair_v1(
    row: dict, lease: dict, record: dict, at: datetime,
    status: tuple[str | None, str | None] = (None, None),
    action: dict | None = None,
    action_rows: list[dict] | None = None,
    needs_chair_open: int = 0,
) -> dict:
    """The console's chair row (holder, state), the lease file, the chair.json record and the edge's reads
    (status line, open action, action-log rows) as the schema-1 chair. Pure: `at` is the one clock.

    `current_action` is the one null the feed allows: no action is running. Day boundaries are Eastern, the
    zone the status line prints in.
    """
    holder = str(row.get("holder") or lease.get("holder") or "")
    # The console row carries the label alone; the lease's holder carries `label@host:pid`.
    full = next((str(h) for h in (row.get("holder"), lease.get("holder")) if h and "@" in str(h)), "")
    host = full.split("@", 1)[1].split(":", 1)[0] if full else ""
    age = beat_age_s(record.get("heartbeat_at"), at)
    last_tick_at, last_status = status
    return {
        "holder": holder,
        "host": host,
        "epoch": int(lease.get("epoch") or 0),
        "liveness": str(row.get("state") or "unknown"),
        "beat_age_s": max(0, age) if age is not None else 0,
        # Neither the console row nor chair.lease.json (holder and epoch only) carries `claude_session`;
        # chair.json does, written by `chair.take` and kept by `chair.beat`.
        "session": str(record.get("claude_session") or "")[:8],
        "last_tick_at": last_tick_at or "",
        "last_status": last_status or "",
        "current_action": action,
        "today": chair_today(action_rows or [], needs_chair_open, at, at.astimezone(EASTERN).utcoffset()),
    }


def _spend_v1(spend: dict) -> dict:
    """An unknown fraction reads 0.0 and an unknown reset time "": the feed's consumers take no nulls."""
    return {
        key: (value if value is not None else (0.0 if key.endswith("_fraction") else ""))
        for key, value in spend.items()
    }


def _machine_v1(row: dict, at: datetime) -> dict:
    """A hosts-table row (with the console's `in_use`) as the schema-1 machine."""
    versions = row.get("versions_json") or {}
    if isinstance(versions, str):
        try:
            versions = json.loads(versions)
        except ValueError:
            versions = {}
    return {
        "name": str(row.get("name") or ""),
        "state": str(row.get("state") or ""),
        "lanes_in_use": int(row.get("in_use") or 0),
        "capacity": int(row.get("capacity") or 0),
        "login_ok": versions.get("login_ok"),
        "login_checked_at": str(versions.get("login_checked_at") or ""),
        "beat_age_s": _age_s(row.get("beat_at"), at),
        "checkouts": {},
    }


def _run_v1(lane: console_screen.LaneRow, local_name: str) -> dict:
    """A live lane as the schema-1 run; a lane with no host of its own runs on this machine."""
    return {
        "run": lane.run,
        "machine": lane.host or local_name,
        "phase": lane.phase or "",
        "node": lane.node or "",
        "attempt": int(lane.attempt or 0),
        "turns": int(lane.turns or 0),
        "cost": float(lane.cost_usd or 0.0),
        "verdict": "",
        "status": "running",
    }


def _queue_v1(row: dict) -> dict:
    return {**row, "priority": int(row.get("priority") or 0), "current_phase": str(row.get("current_phase") or "")}


_REF = re.compile(r"coxswain://([^/]+)/(.*)")


def _inbox_v1(entry: dict) -> dict:
    """A courier entry as the schema-1 inbox row: the reference's kind and id, and the note as the reason."""
    match = _REF.fullmatch(str(entry.get("ref") or ""))
    kind, target = match.groups() if match else ("", str(entry.get("ref") or ""))
    return {"kind": kind, "target": target, "reason": str(entry.get("note") or "")}


def _lease(runs_dir: Path) -> dict:
    try:
        raw = json.loads((runs_dir / "chair.lease.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _chair_record(runs_dir: Path) -> dict:
    """runs/chair.json via `chair.read`; {} when absent or unreadable, as `runs_top_screen.chair_now` does."""
    try:
        record = chair.read(runs_dir)
    except (OSError, json.JSONDecodeError):
        record = None
    return record if isinstance(record, dict) else {}


def _action_rows(runs_dir: Path) -> list[dict]:
    """The chair action log's lines as dicts; [] when it is missing, blank lines and malformed ones skipped."""
    try:
        lines = (runs_dir / ACTION_LOG).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in lines:
        try:
            value = json.loads(line) if line.strip() else None
        except ValueError:
            value = None
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _needs_chair_open(entries: list[dict]) -> int:
    """Unacknowledged courier entries that reach a person and are `needs_chair` refs, before the inbox cap."""
    refs = (_REF.fullmatch(str(e.get("ref") or "")) for e in entries if _reaches_a_person(str(e.get("to") or "")))
    return len([m for m in refs if m and m.group(1) == "needs_chair"])


def _chair_edge(runs_dir: Path) -> dict:
    """Edge: the new chair reads as `_chair_v1` keyword arguments.

    The status line is the newest `status` row in the store; chair-loop.log's last line only when the store has none.
    """
    stored = status_from_store(run_store.latest_chair_status(runs_dir))
    return {
        "status": stored if stored != (None, None) else read_status_record(runs_dir / "chair-loop.log"),
        "action": read_current_action(runs_dir),
        "action_rows": _action_rows(runs_dir),
    }


def gather_feed(runs_dir: Path, work_dir: Path, now: str, profile: dict | None = None) -> dict:
    """Edge: the live schema-1 snapshot from the fleet's existing readers, every section mapped to the shape
    tests/fixtures/dash_feed_v1.json fixes (the contract coxtop parses), with no nulls and nothing json can't write,
    except a machine's `login_ok`, which is null while its login is unchecked. `profile` is the parsed profile
    the capacity chain reads the team cartridge from."""
    runs_dir, work_dir = Path(runs_dir), Path(work_dir)
    at = _parse_now(now)
    local_name, local_capacity = _local_identity(runs_dir, profile or {})
    local_versions = _local_versions(_local_login(local_name), now)
    spend = _spend(runs_dir, now)
    sections = console_screen.gather(
        runs_dir, work_dir, now, local_name, local_capacity, spend, local_versions=local_versions,
    )
    chair_row = sections["chair"][0] if sections["chair"] else {}
    queue, queue_total = _queue(runs_dir)
    blob = _courier_blob(work_dir)
    entries = courier.inbox(blob)
    inbox, inbox_total = _inbox(entries)
    return snapshot(
        now,
        _chair_v1(
            chair_row, _lease(runs_dir), _chair_record(runs_dir), at,
            needs_chair_open=_needs_chair_open(entries), **_chair_edge(runs_dir),
        ),
        _spend_v1(sections["spend"]),
        [_machine_v1(row, at) for row in sections["hosts"]],
        [_run_v1(lane, local_name) for lane in sections["lanes"]],
        [_queue_v1(row) for row in queue],
        queue_total,
        [_inbox_v1(entry) for entry in inbox],
        inbox_total,
        [],
        _open_decisions(entries, blob),
    )
