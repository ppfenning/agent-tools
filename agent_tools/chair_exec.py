"""The thin edge of a chair tick: perform planned actions through injected callables.

Pure helpers decide what an action means; `perform` only fences, dispatches and collects results.
Argv spellings follow `cox runs land --help` and `cox route launch epic|decompose|rescue --help`.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from agent_tools import chair
from agent_tools.chair_types import Action, is_fenced

__all__ = ["Deps", "Result", "argv_for", "branch_pattern", "delete_branches_with", "edge_deps", "landed", "perform"]

Status = Literal["fenced", "dry_run", "skipped", "refused", "recorded", "done", "failed", "landed", "not_landed"]

Run = Callable[[list[str]], tuple[int, str]]


class Result(TypedDict):
    action: Action
    status: Status
    reason: str


@dataclass(frozen=True)
class Deps:
    run: Run  # (exit code, output); the only door to cox
    delete_branches: Callable[[str, str], tuple[list[str], str]]  # (repo, pattern) -> (deleted names, error text)
    acquire_lease: Callable[[str, str], str]  # (holder, host) -> refusal line or ""
    record: Callable[[Action], None]
    run_id: Callable[[Action], str]  # the run whose task a land applies to; "" when unknown
    repo_for: Callable[[Action], str]  # the repository a clear_branches acts in; "" when unknown


_LAUNCH_KINDS = ("relaunch", "retry", "launch_epic", "launch_decompose", "rescue")
_GLOB_CHARS = frozenset("*?[]{}\\ \t")


def landed(code: int, output: str) -> bool:
    """A land counts only when it exits 0 and the output shows both the merge and the mark_done step."""
    return code == 0 and "merge:" in output and "mark_done:" in output


def branch_pattern(initiative: str) -> str | None:
    """`epic/<initiative>/*`, or None when the initiative is empty or could widen the glob."""
    if not initiative or "/" in initiative or _GLOB_CHARS & set(initiative):
        return None
    return f"epic/{initiative}/*"


def argv_for(action: Action, run_id: str = "") -> list[str] | None:
    """The cox argv for a land, launch or pull action; None for any other kind or a missing required field."""
    kind = action.get("kind")
    task, repo, initiative = action.get("task_id", ""), action.get("repo", ""), action.get("initiative", "")
    idea = (action.get("intake_ids") or [""])[0]
    if kind == "land":
        return ["cox", "runs", "land", run_id, "--task", task, "--repo", repo, "--apply"] if run_id and task and repo else None
    if kind == "launch_decompose":
        return ["cox", "route", "launch", "decompose", "--idea", idea, "--initiative-id", initiative or idea] if idea else None
    if kind == "rescue":
        return ["cox", "route", "launch", "rescue", "--initiative", f"work/{initiative}", "--task", task] if initiative and task else None
    if kind in _LAUNCH_KINDS:
        return ["cox", "route", "launch", "epic", "--initiative", initiative, *(["--repo", repo] if repo else [])] if initiative else None
    if kind == "pull":
        return ["cox", "route", "pull"]
    return None


def _result(action: Action, status: Status, reason: str = "") -> Result:
    return {"action": action, "status": status, "reason": reason}


def _land(action: Action, deps: Deps, blocked: dict[str, str]) -> Result:
    repo = action.get("repo", "")
    argv = argv_for(action, deps.run_id(action))
    if argv is None:
        return _result(action, "refused", "land needs a run id, a task_id and a repo")
    if repo in blocked:
        return _result(action, "skipped", f"an earlier land in {repo} ({blocked[repo]}) was not counted")
    code, output = deps.run(argv)
    return _result(action, "landed" if landed(code, output) else "not_landed", output)


def _clear(action: Action, deps: Deps, blocked: dict[str, str]) -> Result:
    initiative = action.get("initiative", "")
    pattern = branch_pattern(initiative)
    repo = deps.repo_for(action)
    if pattern is None:
        return _result(action, "refused", f"branch glob for initiative {initiative!r} is not epic/<initiative>/*")
    if not repo:
        return _result(action, "refused", f"no repository resolved for initiative {initiative}")
    if repo in blocked:
        return _result(action, "skipped", f"an earlier land in {repo} ({blocked[repo]}) was not counted")
    deleted, error = deps.delete_branches(repo, pattern)
    if error:
        return _result(action, "failed", f"deleted {deleted} in {repo}; git: {error.strip()}")
    if not deleted:
        return _result(action, "failed", f"no branch matched {pattern} in {repo}")
    return _result(action, "done", f"deleted {deleted} in {repo}")


def _lease(action: Action, deps: Deps) -> Result:
    line = deps.acquire_lease(action.get("holder", ""), action.get("host", ""))
    return _result(action, "refused", line) if line else _result(action, "done")


def _launch(action: Action, deps: Deps) -> Result:
    argv = argv_for(action)
    if argv is None:
        return _result(action, "refused", f"{action.get('kind')} names no initiative, task or intake id")
    code, output = deps.run(argv)
    return _result(action, "done" if code == 0 else "failed", output)


def _execute(action: Action, deps: Deps, blocked: dict[str, str]) -> Result:
    kind = action.get("kind")
    if kind == "land":
        return _land(action, deps, blocked)
    if kind == "clear_branches":
        return _clear(action, deps, blocked)
    if kind == "take_lease":
        return _lease(action, deps)
    if kind in ("standby", "needs_chair"):
        deps.record(action)
        return _result(action, "recorded")
    if kind in _LAUNCH_KINDS or kind == "pull":
        return _launch(action, deps)
    return _result(action, "refused", f"unsupported action kind {kind!r}")


def perform(actions: list[Action], deps: Deps, current_epoch: Callable[[], int], dry_run: bool) -> list[Result]:
    """Edge. One result per action, in order; the epoch is re-read per action and a dry run touches nothing."""
    results: list[Result] = []
    blocked: dict[str, str] = {}  # repo -> task of the uncounted land that blocks its later lands and deletes
    for action in actions:
        if dry_run:
            results.append(_result(action, "dry_run"))
        elif is_fenced(action, current_epoch()):
            results.append(_result(action, "fenced", "planned under another lease epoch"))
        else:
            result = _execute(action, deps, blocked)
            if action.get("kind") == "land" and result["status"] == "not_landed":
                blocked[action.get("repo", "")] = action.get("task_id", "")
            results.append(result)
    return results


def run_argv(argv: list[str]) -> tuple[int, str]:
    """Edge. A missing binary is exit 127 with its message, never an exception out of perform."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as error:
        return 127, f"{argv[0] if argv else '<empty argv>'}: {error}"
    return done.returncode, done.stdout + done.stderr


def delete_branches_with(run: Run, repo: str, pattern: str) -> tuple[list[str], str]:
    """Force-delete local branches under `pattern` in `repo`; (deleted names, error text)."""
    code, listing = run(["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}"])
    if code != 0:
        return [], listing or f"git for-each-ref exited {code}"
    prefix = pattern.removesuffix("*")
    outcomes = [(name, run(["git", "-C", repo, "branch", "-D", name])) for name in listing.split() if name.startswith(prefix)]
    deleted = [name for name, (c, _) in outcomes if c == 0]
    return deleted, "".join(out for _, (c, out) in outcomes if c != 0)


def edge_deps(
    runs_dir: Path,
    session: str,
    pid: int,
    run_id: Callable[[Action], str],
    repo_for: Callable[[Action], str],
    record: Callable[[Action], None],
) -> Deps:
    """Edge. The real bundle: subprocess for cox and git, chair.acquire_lease for the lease."""
    return Deps(
        run=run_argv,
        delete_branches=lambda repo, pattern: delete_branches_with(run_argv, repo, pattern),
        acquire_lease=lambda holder, host: chair.acquire_lease(runs_dir, session, pid, host),
        record=record,
        run_id=run_id,
        repo_for=repo_for,
    )
