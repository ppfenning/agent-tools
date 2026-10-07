"""Wrapper for graphs' `python -m harness.store_cli`: pure argv builders, pure parsers, one thin edge.

The contract, from graphs-store-write-cli. Every command runs as
`<harness_dir>/.venv/bin/python -m harness.store_cli <command...>` and prints one JSON
object on stdout. Exit 0 is success, 3 a refused precondition, 2 bad arguments or an
unreadable store. `--store-url <url>` is optional; every edge below that takes a `runs_dir`
always passes the store `run_store` resolves for it, so both sides use one store (without it
the harness falls back to a `cox.db` in its own checkout). `pause`/`resume` take an explicit
`store_url` instead of a `runs_dir`, since a run may be paused from a machine that never
ran it; the caller must pass one when the run's store is not the harness's own default, or
a pause can silently land against the wrong store.

    mark-landed <run_id> <phase> <task> --pr <url> --at <iso>
        exit 0 -> the task record as a JSON object; exit 3 -> the record is not in the store
    lease acquire <name> <holder> --ttl <seconds>
    lease renew   <name> <holder> <epoch> --ttl <seconds>
    lease release <name> <holder> <epoch>
        exit 0 -> {"ok": true, "epoch": <int>, "holder": "<holder>"}
        exit 0 -> {"ok": true, "epoch": null, "holder": null} when a release finds no holder
        exit 3 -> {"ok": false, "epoch": <int or null>, "holder": "<current holder or null>"}

    set-state <initiative> <task> <state> --by <who> [--expect <state>]
        exit 0 -> the task record as a JSON object; exit 3 -> a refused precondition
        exit 3 may carry the store's current state under "state"; the key is assumed, not yet confirmed

    pause <run_id> [--reason <text>]
        exit 0 -> {"ok": true, "run": "<run_id>", "paused": true, "reason": "<text or null>"}
        exit 3 -> {"ok": false, "run": "<run_id>", "paused": null, "reason": null}
    resume <run_id>
        exit 0 -> {"ok": true, "run": "<run_id>", "paused": false, "reason": null}
        exit 3 -> {"ok": false, "run": "<run_id>", "paused": null, "reason": null}

Exit 3 is a result, not an error. Lease names are the caller's value, except a land lease, named by land_lease_name.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_tools.run_store import _harness_python, _store_url

STORE_TIMEOUT_S = 60


@dataclass(frozen=True)
class Landed:
    record: dict[str, Any]


@dataclass(frozen=True)
class NotInStore:
    pass


@dataclass(frozen=True)
class Failed:
    code: int
    detail: str


@dataclass(frozen=True)
class LeaseGranted:
    epoch: int
    holder: str


@dataclass(frozen=True)
class LeaseRefused:
    epoch: int | None
    holder: str | None


@dataclass(frozen=True)
class LeaseReleased:
    pass


@dataclass(frozen=True)
class LeaseError:
    detail: str


@dataclass(frozen=True)
class StateSet:
    record: dict[str, Any]


@dataclass(frozen=True)
class StateRefused:
    detail: str
    current: str | None = None


@dataclass(frozen=True)
class NotAvailable:
    reason: str = ""


@dataclass(frozen=True)
class Paused:
    reason: str | None


@dataclass(frozen=True)
class PauseRefused:
    pass


@dataclass(frozen=True)
class Resumed:
    pass


@dataclass(frozen=True)
class ResumeRefused:
    pass


MarkLandedResult = Landed | NotInStore | Failed | NotAvailable
SetStateResult = StateSet | StateRefused | Failed | NotAvailable
LeaseResult = LeaseGranted | LeaseReleased | LeaseRefused | LeaseError | NotAvailable
PauseResult = Paused | PauseRefused | Failed | NotAvailable
ResumeResult = Resumed | ResumeRefused | Failed | NotAvailable

_MODULE = ["-m", "harness.store_cli"]


def _store(store_url: str | None) -> list[str]:
    return ["--store-url", store_url] if store_url else []


def mark_landed_argv(python: str, run_id: str, phase: str, task: str, pr: str, at: str, store_url: str | None = None) -> list[str]:
    return [python, *_MODULE, "mark-landed", run_id, phase, task, "--pr", pr, "--at", at, *_store(store_url)]


def set_state_argv(
    python: str, initiative: str, task: str, state: str, by: str, store_url: str | None = None, expected: str | None = None
) -> list[str]:
    expect = ["--expect", expected] if expected is not None else []
    return [python, *_MODULE, "set-state", initiative, task, state, "--by", by, *expect, *_store(store_url)]


def land_lease_name(task: str) -> str:
    return f"land:{task}"


def lease_acquire_argv(python: str, name: str, holder: str, ttl: int, store_url: str | None = None, steal: bool = False) -> list[str]:
    return [python, *_MODULE, "lease", "acquire", name, holder, "--ttl", str(ttl), *(["--steal"] if steal else []), *_store(store_url)]


def lease_renew_argv(python: str, name: str, holder: str, epoch: int, ttl: int, store_url: str | None = None) -> list[str]:
    return [python, *_MODULE, "lease", "renew", name, holder, str(epoch), "--ttl", str(ttl), *_store(store_url)]


def lease_release_argv(python: str, name: str, holder: str, epoch: int, store_url: str | None = None) -> list[str]:
    return [python, *_MODULE, "lease", "release", name, holder, str(epoch), *_store(store_url)]


def pause_argv(run_id: str, reason: str | None = None, store_url: str | None = None) -> list[str]:
    return ["pause", run_id, *(["--reason", reason] if reason is not None else []), *_store(store_url)]


def resume_argv(run_id: str, store_url: str | None = None) -> list[str]:
    return ["resume", run_id, *_store(store_url)]


def _json_object(stdout: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_mark_landed(code: int, stdout: str) -> Landed | NotInStore | Failed:
    """Exit 0 with a JSON object is the record; exit 3 is not-in-store; anything else fails."""
    record = _json_object(stdout)
    if code == 3:
        return NotInStore()
    if code == 0 and record is not None:
        return Landed(record)
    return Failed(code, stdout.strip() or "no JSON object on stdout")


def parse_set_state(code: int, stdout: str, stderr: str = "") -> StateSet | StateRefused | Failed:
    """Exit 3 is a refused precondition; exit 0 with a JSON object is the record; every other outcome fails with its detail."""
    record = _json_object(stdout)
    detail = stdout.strip() or stderr.strip()
    if code == 3:
        current = record.get("state") if record is not None else None
        return StateRefused(detail, current if isinstance(current, str) else None)
    if code == 0 and record is not None:
        return StateSet(record)
    return Failed(code, detail or "no JSON object on stdout")


def parse_lease(code: int, stdout: str) -> LeaseGranted | LeaseReleased | LeaseRefused | LeaseError:
    """Exit 0 is granted, or released when epoch and holder are both present and null; exit 3 is refused; the rest are errors."""
    body = _json_object(stdout)
    if body is None:
        return LeaseError(f"exit {code}: {stdout.strip() or 'no JSON object on stdout'}")
    if code == 0 and body.get("ok") is True and isinstance(body.get("epoch"), int) and isinstance(body.get("holder"), str):
        return LeaseGranted(body["epoch"], body["holder"])
    if code == 0 and body.get("ok") is True and all(k in body and body[k] is None for k in ("epoch", "holder")):
        return LeaseReleased()
    if code == 3:
        return LeaseRefused(body.get("epoch"), body.get("holder"))
    return LeaseError(f"exit {code}: {stdout.strip()}")


def parse_pause(code: int, stdout: str) -> Paused | PauseRefused | Failed:
    """Exit 3 is refused; exit 0 with a JSON object is paused, carrying the reason if any; anything else fails."""
    if code == 3:
        return PauseRefused()
    body = _json_object(stdout)
    if code == 0 and body is not None and body.get("ok") is True:
        reason = body.get("reason")
        return Paused(reason if isinstance(reason, str) else None)
    return Failed(code, stdout.strip() or "no JSON object on stdout")


def parse_resume(code: int, stdout: str) -> Resumed | ResumeRefused | Failed:
    """Exit 3 is refused; exit 0 with a JSON object is resumed; anything else fails."""
    if code == 3:
        return ResumeRefused()
    body = _json_object(stdout)
    if code == 0 and body is not None and body.get("ok") is True:
        return Resumed()
    return Failed(code, stdout.strip() or "no JSON object on stdout")


def _run(
    build: Any, run: Callable[..., subprocess.CompletedProcess[str]] | None = None
) -> tuple[int, str] | NotAvailable | None:
    """Edge. Run the argv `build(python)` returns; None when the harness is missing. A failed spawn is code -1.

    A command still running after `STORE_TIMEOUT_S` seconds is NotAvailable, its reason naming the argv.
    `run` defaults to `subprocess.run`, looked up at call time.
    """
    python = _harness_python()
    if python is None:
        return None
    argv = build(str(python))
    try:
        done = (run or subprocess.run)(argv, capture_output=True, text=True, check=False, timeout=STORE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return NotAvailable(f"{' '.join(argv)} timed out after {STORE_TIMEOUT_S} s")
    except OSError as exc:
        return -1, str(exc)
    return done.returncode, done.stdout or done.stderr


def _outcome(ran: tuple[int, str] | NotAvailable | None, parse: Callable[[int, str], Any]) -> Any:
    """Pure. NotAvailable for a missing harness or a timeout; else `parse` of the exit code and output."""
    if ran is None:
        return NotAvailable()
    return ran if isinstance(ran, NotAvailable) else parse(*ran)


def runner(runs_dir: Path) -> Callable[[list[str]], tuple[int, str]]:
    """Edge. A function that runs `store_cli <args>` against the store for `runs_dir`; a missing harness is code 1."""
    url = _store_url(Path(runs_dir))

    def run(args: list[str]) -> tuple[int, str]:
        ran = _run(lambda python: [python, *_MODULE, *args, *_store(url)])
        if ran is None:
            return 1, "harness not available"
        return (1, ran.reason) if isinstance(ran, NotAvailable) else ran

    return run


def mark_landed(runs_dir: Path, run_id: str, phase: str, task: str, pr: str, at: str) -> MarkLandedResult:
    url = _store_url(Path(runs_dir))
    ran = _run(lambda python: mark_landed_argv(python, run_id, phase, task, pr, at, url))
    return _outcome(ran, parse_mark_landed)


def pause(
    run_id: str,
    reason: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    store_url: str | None = None,
) -> PauseResult:
    """Edge. `store_url` targets the run's store explicitly; omitting it risks the harness's own default store."""
    ran = _run(lambda python: [python, *_MODULE, *pause_argv(run_id, reason, store_url)], run)
    return _outcome(ran, parse_pause)


def resume(
    run_id: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    store_url: str | None = None,
) -> ResumeResult:
    """Edge. `store_url` targets the run's store explicitly; omitting it risks the harness's own default store."""
    ran = _run(lambda python: [python, *_MODULE, *resume_argv(run_id, store_url)], run)
    return _outcome(ran, parse_resume)


def set_state(runs_dir: Path, initiative: str, task: str, state: str, by: str, expected: str | None = None) -> SetStateResult:
    url = _store_url(Path(runs_dir))
    ran = _run(lambda python: set_state_argv(python, initiative, task, state, by, url, expected))
    return _outcome(ran, parse_set_state)


def _warning(initiative: str, task: str, state: str, reason: str) -> str:
    return " ".join(f"warning: store did not record {initiative}/{task} as {state}: {reason}".split())


def mirror_state(runs_dir: Path, initiative: str, task: str, state: str, by: str) -> str | None:
    """Never raises. None when the store took the state or is absent; else one `warning:` line for the caller to print."""
    try:
        result = set_state(runs_dir, initiative, task, state, by)
    except Exception as exc:
        return _warning(initiative, task, state, f"{type(exc).__name__}: {exc}")
    if isinstance(result, StateRefused):
        return _warning(initiative, task, state, f"refused: {result.detail or 'no detail'}")
    if isinstance(result, Failed):
        return _warning(initiative, task, state, f"exit {result.code}: {result.detail}")
    return None


def lease_acquire(runs_dir: Path, name: str, holder: str, ttl: int, steal: bool = False) -> LeaseResult:
    url = _store_url(Path(runs_dir))
    ran = _run(lambda python: lease_acquire_argv(python, name, holder, ttl, url, steal))
    return _outcome(ran, parse_lease)


def lease_renew(runs_dir: Path, name: str, holder: str, epoch: int, ttl: int) -> LeaseResult:
    url = _store_url(Path(runs_dir))
    ran = _run(lambda python: lease_renew_argv(python, name, holder, epoch, ttl, url))
    parsed = _outcome(ran, parse_lease)
    # graphs' harness/store_cli_lease.lease_renew answers `{"ok": true, "epoch": null, "holder": null}`, the release
    # shape, because a renew keeps the epoch. So a renew that reads as released is granted at the caller's epoch.
    return LeaseGranted(epoch, holder) if isinstance(parsed, LeaseReleased) else parsed


def lease_release(runs_dir: Path, name: str, holder: str, epoch: int) -> LeaseResult:
    url = _store_url(Path(runs_dir))
    ran = _run(lambda python: lease_release_argv(python, name, holder, epoch, url))
    return _outcome(ran, parse_lease)
