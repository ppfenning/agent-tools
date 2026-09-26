"""The chair lock, `runs/chair.json`: pure liveness/take/beat/release/clear over the (session, pid, host) triple; a malformed record reads as stale."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import socket
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_tools import store_cli

__all__ = [
    "CHAIR_FILENAME",
    "DEFAULT_HEARTBEAT_MINUTES",
    "DEFAULT_LEASE_TTL_SECONDS",
    "LEASE_FILENAME",
    "LEASE_NAME",
    "acquire_lease",
    "beat",
    "beat_loop",
    "chair_path",
    "claude_session_from_env",
    "clear",
    "expired",
    "extend",
    "guard",
    "hours_left",
    "leader_path",
    "lease_holder",
    "lease_verdict",
    "liveness",
    "locked",
    "pid_alive",
    "read",
    "release",
    "release_lease",
    "renew_lease",
    "take",
    "write",
]

CHAIR_FILENAME = "chair.json"
_LEGACY_LEADER_FILENAME = "leader.json"

DEFAULT_HEARTBEAT_MINUTES = 10

LEASE_NAME = "chair"
# Beside chair.json, never inside it: the lock file format is unchanged. Holds the holder and epoch that renew and release need.
LEASE_FILENAME = "chair.lease.json"
DEFAULT_LEASE_TTL_SECONDS = DEFAULT_HEARTBEAT_MINUTES * 60


def _is_holder(record: dict[str, Any], session: str, pid: int, host: str) -> bool:
    """A caller holds `record` only when its session, pid and host all match what     was recorded — the label alone is not proof of identity."""
    return record.get("session") == session and record.get("pid") == pid and record.get("host") == host


def liveness(record: dict[str, Any] | None, pid_alive_: bool, now: datetime.datetime, host: str, heartbeat_minutes: int = DEFAULT_HEARTBEAT_MINUTES) -> str:
    """Pure: pid_alive_ is consulted only when record's host is host; a fresh heartbeat from another host is live regardless."""
    if record is None:
        return "none"
    try:
        heartbeat_at = datetime.datetime.fromisoformat(record["heartbeat_at"])
        age = now - heartbeat_at
    except (KeyError, TypeError, ValueError):
        return "stale"
    if age > datetime.timedelta(minutes=heartbeat_minutes):
        return "stale"
    if record.get("host") == host and not pid_alive_:
        return "crashed"
    return "live"


def _until(record: dict[str, Any] | None) -> datetime.datetime | None:
    """The record's `until` as an aware datetime; a missing, malformed or naive one reads as None."""
    try:
        parsed = datetime.datetime.fromisoformat(record["until"])
    except (KeyError, TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def expired(record: dict[str, Any] | None, now: datetime.datetime) -> bool:
    """Pure: the record carries an `until` before `now`. No `until` never expires."""
    until = _until(record)
    return until is not None and until < now


def hours_left(record: dict[str, Any] | None, now: datetime.datetime) -> float | None:
    """Pure: hours from `now` to the record's `until`, negative once past; None with no usable `until`."""
    until = _until(record)
    return None if until is None else (until - now).total_seconds() / 3600


def extend(record: dict[str, Any], now: datetime.datetime, hours: float) -> dict[str, Any]:
    """Pure: a copy of `record` whose `until` is `hours` after `now`."""
    return {**record, "until": (now + datetime.timedelta(hours=hours)).isoformat()}


def _held_by_line(record: dict[str, Any]) -> str:
    until = f" until {record['until']}" if "until" in record else ""
    return f"held by {record.get('session', '?')} (pid {record.get('pid', '?')}) on {record.get('host', '?')}{until}"


def take(
    record: dict[str, Any] | None,
    session: str,
    pid: int,
    host: str,
    now: datetime.datetime,
    heartbeat_minutes: int,
    pid_alive_: bool,
    steal: bool = False,
    claude_session: str | None = None,
    hours: float | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Pure. A live record past its `until` is taken like a stale one. `hours` stamps the new record's `until`."""
    state = liveness(record, pid_alive_, now, host, heartbeat_minutes)
    lapsed = state == "live" and expired(record, now)
    if state == "live" and not lapsed:
        return None, f"chair: {_held_by_line(record)}"
    if (lapsed or state in ("stale", "crashed")) and not steal:
        why = f"takeover expired at {record['until']}" if lapsed else state
        return None, f"chair: {_held_by_line(record)} ({why}; pass --steal to take over)"
    taken_at = now.isoformat()
    new_record = {"session": session, "pid": pid, "host": host, "taken_at": taken_at, "heartbeat_at": taken_at, "runs": [], "claude_session": claude_session}
    return (new_record if hours is None else extend(new_record, now, hours)), ""


def same_holder(record: dict[str, Any] | None, session: str, pid: int, host: str) -> bool:
    """Pure: the record names this session, pid and host. No record names no one."""
    return record is not None and _is_holder(record, session, pid, host)


def claude_session_from_env(environ: Mapping[str, str]) -> str | None:
    """Edge helper. The chair's Claude session id from CLAUDE_CODE_SESSION_ID, else CLAUDE_SESSION_ID, else None; absence never fails."""
    return environ.get("CLAUDE_CODE_SESSION_ID") or environ.get("CLAUDE_SESSION_ID") or None


def beat(
    record: dict[str, Any] | None,
    session: str,
    pid: int,
    host: str,
    now: datetime.datetime,
    run_id: str | None = None,
    claude_session: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Pure. A None `claude_session` keeps the recorded one, so a beat from a shell without the env var does not erase it."""
    if record is None or not _is_holder(record, session, pid, host):
        return None, f"chair: not held by {session} (pid {pid}) on {host}"
    existing_runs = record.get("runs", [])
    runs = existing_runs if run_id is None or run_id in existing_runs else [*existing_runs, run_id]
    kept = claude_session if claude_session is not None else record.get("claude_session")
    return {**record, "heartbeat_at": now.isoformat(), "runs": runs, "claude_session": kept}, ""


def beat_loop(
    label: str,
    pid: int,
    *,
    runs_dir: Path | str,
    interval: float | None = None,
    clock=time,
    alive=os.kill,
    environ: Mapping[str, str] = os.environ,
) -> int:
    """Edge. Beats the chair at `runs_dir` for `label`/`pid` every `interval`
    seconds (default: half of DEFAULT_HEARTBEAT_MINUTES) while `pid` is alive;
    prints `beat`'s reason and returns 2 the first time it can't renew, 0 once
    `alive(pid, 0)` raises `ProcessLookupError`. A `PermissionError` from
    `alive` means the pid is alive but unsignallable, same as `pid_alive`."""
    tick = DEFAULT_HEARTBEAT_MINUTES * 60 / 2 if interval is None else interval
    host = socket.gethostname()
    while True:
        try:
            alive(pid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            pass
        with locked(runs_dir):
            new_record, reason = beat(read(runs_dir), label, pid, host, datetime.datetime.now(datetime.UTC), claude_session=claude_session_from_env(environ))
            if new_record is None:
                print(f"chair: {reason}")
                return 2
            lost = renew_lease(runs_dir, label, pid, host)
            if lost:
                print(lost)
                return 2
            write(runs_dir, new_record)
        clock.sleep(tick)


def release(record: dict[str, Any] | None, session: str, pid: int, host: str) -> tuple[dict[str, Any] | None, str]:
    """Pure."""
    if record is None:
        return None, "chair: no lock held"
    if not _is_holder(record, session, pid, host):
        return record, f"chair: not held by {session} (pid {pid}) on {host}"
    return None, ""


def guard(record: dict | None, holder: str, state: str) -> str | None:
    """The one-line refusal when a LIVE lock belongs to another holder, else None."""
    if record is None or state != "live" or record.get("session") == holder:
        return None
    return f"refusing: the landing loop is held by {record.get('session')} (live); pass --force to override"


def chair_path(runs_dir: Path) -> Path:
    return Path(runs_dir) / CHAIR_FILENAME


# Back-compat alias for the pre-rename name; consumers still spell this
# `leader_path` until the wire-consumers phase moves them to `chair_path`.
leader_path = chair_path


def read(runs_dir: Path) -> dict[str, Any] | None:
    """Edge. Tries chair.json first; falls back to leader.json so a lock taken before this rename is not orphaned."""
    runs_dir = Path(runs_dir)
    try:
        text = chair_path(runs_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        try:
            text = (runs_dir / _LEGACY_LEADER_FILENAME).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
    return json.loads(text)


def write(runs_dir: Path, record: dict[str, Any] | None) -> None:
    """Edge. Always clears the legacy leader.json too, so a lock read through the fallback is actually released, and a beat leaves at most one lock file behind."""
    runs_dir = Path(runs_dir)
    path = chair_path(runs_dir)
    legacy = runs_dir / _LEGACY_LEADER_FILENAME
    if record is None:
        path.unlink(missing_ok=True)
        legacy.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    legacy.unlink(missing_ok=True)


def lease_holder(session: str, pid: int, host: str) -> str:
    """Pure. The one holder string the store lease sees for a (session, pid, host) triple."""
    return f"{session}@{host}:{pid}"


def lease_verdict(result: store_cli.LeaseResult) -> tuple[str, str]:
    """Pure. ("granted" | "refused" | "fallback", line). A refusal is a foreign holder; a lease error or a missing harness falls back to the lock file with a warning."""
    if isinstance(result, store_cli.LeaseGranted):
        return "granted", ""
    if isinstance(result, store_cli.LeaseRefused):
        return "refused", f"chair: held by {result.holder} (store lease)" if result.holder else "chair: the store lease was refused"
    if isinstance(result, store_cli.LeaseError):
        return "fallback", f"chair: warning: store lease failed ({result.detail}); using the lock file only"
    if isinstance(result, store_cli.LeaseReleased):
        return "fallback", "chair: warning: store lease answered released, not granted; using the lock file only"
    return "fallback", "chair: warning: no harness found; using the lock file only"


def _lease_path(runs_dir: Path) -> Path:
    return Path(runs_dir) / LEASE_FILENAME


def _read_lease(runs_dir: Path, holder: str) -> dict[str, Any] | None:
    """Edge. The sidecar record when it is this holder's and carries an integer epoch, else None."""
    try:
        parsed = json.loads(_lease_path(runs_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if isinstance(parsed, dict) and parsed.get("holder") == holder and isinstance(parsed.get("epoch"), int):
        return parsed
    return None


def _write_lease(runs_dir: Path, holder: str, epoch: int) -> None:
    path = _lease_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"holder": holder, "epoch": epoch}), encoding="utf-8")


def acquire_lease(runs_dir: Path, session: str, pid: int, host: str, ttl: int = DEFAULT_LEASE_TTL_SECONDS, steal: bool = False) -> str:
    """Edge. The refusal line when another holder has a live lease, else "". A missing harness or a lease error warns on stderr and returns "", so the lock file alone governs. `steal` takes a held lease at a higher epoch."""
    holder = lease_holder(session, pid, host)
    result = store_cli.lease_acquire(runs_dir, LEASE_NAME, holder, ttl, steal)
    if isinstance(result, store_cli.LeaseGranted):
        _write_lease(runs_dir, holder, result.epoch)
        return ""
    verdict, line = lease_verdict(result)
    if verdict == "refused":
        return line
    _lease_path(runs_dir).unlink(missing_ok=True)
    print(line, file=sys.stderr)
    return ""


def renew_lease(runs_dir: Path, session: str, pid: int, host: str, ttl: int = DEFAULT_LEASE_TTL_SECONDS) -> str:
    """Edge. The refusal line when the lease is lost to another holder, else "". With no sidecar (a chair taken before
    the lease existed, or one that fell back to the lock file), the beat tries to acquire it instead."""
    holder = lease_holder(session, pid, host)
    lease = _read_lease(runs_dir, holder)
    if lease is None:
        return acquire_lease(runs_dir, session, pid, host, ttl)
    result = store_cli.lease_renew(runs_dir, LEASE_NAME, holder, lease["epoch"], ttl)
    if isinstance(result, store_cli.LeaseGranted):
        _write_lease(runs_dir, holder, result.epoch)
        return ""
    verdict, line = lease_verdict(result)
    if verdict == "refused":
        return line
    print(line, file=sys.stderr)
    return ""


def release_lease(runs_dir: Path, session: str, pid: int, host: str) -> None:
    """Edge. Releases the lease and removes the sidecar; only a refusal or a fallback warns, and never blocks."""
    holder = lease_holder(session, pid, host)
    lease = _read_lease(runs_dir, holder)
    if lease is None:
        return
    result = store_cli.lease_release(runs_dir, LEASE_NAME, holder, lease["epoch"])
    if not isinstance(result, (store_cli.LeaseGranted, store_cli.LeaseReleased)):
        print(lease_verdict(result)[1], file=sys.stderr)
    _lease_path(runs_dir).unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    # Chair lock only: a reused pid reads alive here, and the heartbeat in `liveness` is what catches it. Run pidfiles use `epic.run_live`.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def clear(runs_dir: Path, force: bool = False) -> tuple[Path, dict[str, Any]] | None:
    """Edge. Removes whichever lock file is present unless its recorded pid is live and force is False; returns the path and holder removed, or None."""
    runs_dir = Path(runs_dir)
    primary = chair_path(runs_dir)
    path = primary if primary.exists() else runs_dir / _LEGACY_LEADER_FILENAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if pid_alive(record.get("pid")) and not force:
        return None
    path.unlink(missing_ok=True)
    return path, record


@contextlib.contextmanager
def locked(runs_dir: Path):
    """Edge."""
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(runs_dir / "chair.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="python -m agent_tools.chair")
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    beat_loop_parser = subparsers.add_parser("beat-loop", help="beat the chair for --label/--pid until the pid is gone")
    beat_loop_parser.add_argument("--label", required=True)
    beat_loop_parser.add_argument("--pid", type=int, required=True)
    beat_loop_parser.add_argument("--runs-dir", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(beat_loop(args.label, args.pid, runs_dir=args.runs_dir))
