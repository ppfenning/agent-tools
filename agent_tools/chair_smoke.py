"""Post-land smoke: pure verdict and hold-record core, plus command, hold-file, and revert-PR edges.

After a land, the chair runs a fixed sequence of smoke commands against the repo. The pure core
here decides whether they passed and builds the record held when they did not; the edges run the
commands, persist and clear that record, and build then run the revert-PR commands. Every edge
takes the values it needs as arguments; nothing here reads a global, the clock, or the environment.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_tools.chair_types import HoldRecord, LandTrigger, SmokeCommandResult

__all__ = [
    "clear_hold",
    "hold_path",
    "hold_record",
    "read_hold",
    "revert_pr_argv",
    "run_revert_pr",
    "run_smoke_commands",
    "smoke_result",
    "smoke_verdict",
    "write_hold",
]

_SMOKE_TIMEOUT_S = 120
_TAIL_LINES = 20
_REVERT_STEPS = ("checkout", "revert", "push", "pr_create")


def smoke_verdict(results: list[SmokeCommandResult]) -> tuple[bool, SmokeCommandResult | None]:
    """True with None when every result is ok; else False with the first not-ok result."""
    for result in results:
        if not result["ok"]:
            return False, result
    return True, None


def hold_record(land: LandTrigger, failing: SmokeCommandResult) -> HoldRecord:
    """The HoldRecord for a failing smoke result, tail cut to at most its last 20 lines."""
    lines = failing["tail"].splitlines()
    return {
        "land": dict(land),
        "failing_command": failing["command"],
        "tail": "\n".join(lines[-_TAIL_LINES:]),
        "cause": "smoke_failed",
    }


def smoke_result(command: list[str], returncode: int, output: str, timed_out: bool) -> SmokeCommandResult:
    """Pure. ok is True only when the process exited 0, 'Traceback' is absent from output, and it did not time out."""
    ok = not timed_out and returncode == 0 and "Traceback" not in output
    return {"command": command, "ok": ok, "tail": output}


def _run_argv(argv: list[str], cwd: str, timeout: float) -> SmokeCommandResult:
    """Edge. A missing binary or a timeout counts as not ok, via smoke_result; never raises out of run_smoke_commands."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd, timeout=timeout)
    except OSError as error:
        return smoke_result(argv, 127, f"{argv[0] if argv else '<empty argv>'}: {error}", timed_out=False)
    except subprocess.TimeoutExpired:
        return smoke_result(argv, 124, f"{argv[0] if argv else '<empty argv>'}: timed out after {timeout}s", timed_out=True)
    return smoke_result(argv, done.returncode, done.stdout + done.stderr, timed_out=False)


def run_smoke_commands(repo_dir: str) -> list[SmokeCommandResult]:
    """Edge. Three fresh subprocesses, each with a 120-second timeout: route context, runs top, chair run dry-run."""
    commands = [
        ["cox", "route", "context"],
        ["cox", "runs", "top", "--once"],
        ["cox", "chair", "run", "--once", "--dry-run"],
    ]
    return [_run_argv(argv, repo_dir, _SMOKE_TIMEOUT_S) for argv in commands]


def hold_path(runs_dir: str) -> Path:
    return Path(runs_dir) / "chair.hold.json"


def write_hold(runs_dir: str, record: HoldRecord) -> None:
    """Edge."""
    hold_path(runs_dir).write_text(json.dumps(record), encoding="utf-8")


def read_hold(runs_dir: str) -> HoldRecord | None:
    """Edge. None when the file is absent."""
    try:
        return json.loads(hold_path(runs_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def clear_hold(runs_dir: str) -> None:
    """Edge. No error when the file is already absent."""
    hold_path(runs_dir).unlink(missing_ok=True)


def revert_pr_argv(land: LandTrigger, failing_tail: str) -> dict:
    """Pure. The argv for the four commands the caller runs in order to open a revert PR."""
    repo = land["repo"]
    branch = f"revert/{land['pr']}"
    return {
        "checkout": ["git", "-C", repo, "checkout", "-b", branch, "origin/main"],
        "revert": ["git", "-C", repo, "revert", "--no-edit", land["commit"]],
        "push": ["git", "-C", repo, "push", "origin", branch],
        "pr_create": [
            "gh", "pr", "create", "--repo", repo,
            "--title", f"Revert #{land['pr']}: post-land smoke failed",
            "--body", failing_tail,
            "--head", branch,
        ],
    }


def run_revert_pr(argv: dict) -> None:
    """Edge. Runs the four commands in order and raises CalledProcessError at the first failure, so a
    failed checkout never lets revert commit onto whatever branch is checked out. Never merges the PR."""
    for key in _REVERT_STEPS:
        subprocess.run(argv[key], check=True)
