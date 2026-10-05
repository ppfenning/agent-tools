"""Perform a `review_landed` action: the task is done, stamped landed, and its agents/ branch is deleted.

`review_steps` is pure and `apply_review` is the thin edge. Every step is safe to repeat, and a failed step stops
the sequence so the next tick retries from the top. Only the task's `agents/<run>/<task>` branch is deleted, on the
chair checkout and, when the owning run has a host, on that host. Worktrees and `epic/` branches belong to the
clear path after the phase lands.
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from agent_tools.chair_types import Action
from agent_tools.store_cli import Failed, Landed, StateSet

NO_RUN_CAUSE = "review_no_run"
NO_HOST_CAUSE = "review_no_host"
NO_SSH_CAUSE = "review_no_ssh"


@dataclass(frozen=True)
class SetDone:
    initiative: str
    task_id: str


@dataclass(frozen=True)
class MarkLanded:
    run: str
    phase: str
    task_id: str
    pr: str
    at: str | None  # the forge's merge time; None means the time the chair saw it merged


@dataclass(frozen=True)
class DeleteBranch:
    repo: str
    pattern: str
    host: str  # "" is the chair checkout


Step = SetDone | MarkLanded | DeleteBranch


@dataclass(frozen=True)
class Outcome:
    status: Literal["done", "failed", "needs_chair"]
    reason: str
    cause: str = ""


def refusal(action: Action) -> str:
    """The needs_chair cause for an action the executor must not guess about; "" when it names its run and host.

    An absent `host` key is a run with no recorded host. An empty one is the chair machine.
    """
    if not action.get("run"):
        return NO_RUN_CAUSE
    return "" if "host" in action else NO_HOST_CAUSE


def review_steps(action: Action) -> list[Step]:
    """Set done, stamp landed with the PR url and merge time, then delete the agents/ branch here and on the host."""
    run, task_id, repo, host = action["run"], action["task_id"], action["repo"], action.get("host", "")
    pattern = f"agents/{run}/{task_id}"
    return [
        SetDone(action["initiative"], task_id),
        MarkLanded(run, action["phase"], task_id, action["url"], action.get("merged_at")),
        DeleteBranch(repo, pattern, ""),
        *([DeleteBranch(repo, pattern, host)] if host else []),
    ]


def _set_done(result: object) -> str:
    """"" when the store holds the task as done, else why it does not. A refusal that names done is a repeat."""
    if isinstance(result, StateSet):
        return ""
    if getattr(result, "current", None) == "done":
        return ""
    return f"set-state done: {result}"


def _stamped(result: object) -> str:
    """"" when the record is landed. The store saying it was already landed is the same as Landed."""
    if isinstance(result, Landed):
        return ""
    if isinstance(result, Failed) and "already landed" in result.detail.lower():
        return ""
    return f"mark-landed: {result}"


def apply_review(
    action: Action,
    *,
    set_state: Callable[[str, str, str, str], object],  # (initiative, task, state, by) -> a store_cli SetStateResult
    mark_landed: Callable[[str, str, str, str, str], object],  # (run, phase, task, pr, at) -> a store_cli MarkLandedResult
    delete_branches: Callable[[str, str, str], tuple[list[str], str]],  # (ssh, repo, pattern); ssh "" is here
    ssh_for: Callable[[str], str],
    now: Callable[[], str],
) -> Outcome:
    """Edge. Runs `review_steps` in order and stops at the first failure, leaving later steps unrun."""
    cause = refusal(action)
    if cause:
        return Outcome("needs_chair", f"review_landed for {action.get('task_id', '')}: {cause}", cause)
    host = action.get("host", "")
    ssh = ssh_for(host) if host else ""
    if host and not ssh:
        return Outcome("needs_chair", f"no ssh destination for host {host}: its agents/ branch cannot be deleted", NO_SSH_CAUSE)
    notes: list[str] = []
    for step in review_steps(action):
        if isinstance(step, SetDone):
            error = _set_done(set_state(step.initiative, step.task_id, "done", "chair"))
        elif isinstance(step, MarkLanded):
            error = _stamped(mark_landed(step.run, step.phase, step.task_id, step.pr, step.at or now()))
        else:
            deleted, error = delete_branches(ssh if step.host else "", step.repo, step.pattern)
            notes += [f"deleted {name}{f' on {step.host}' if step.host else ''}" for name in deleted]
        if error:
            return Outcome("failed", error.strip())
    return Outcome("done", "; ".join(notes) or "task done and landed; no agents/ branch to delete")
