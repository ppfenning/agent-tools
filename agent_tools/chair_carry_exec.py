"""The thin edge of a carry: perform one carry_phase through injected git, forge and store ports.

The decisions and the stop wording live in `chair_carry`; this module only sequences the calls.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agent_tools import chair_carry
from agent_tools.chair_types import Action
from agent_tools.land import pr_footer

if TYPE_CHECKING:  # the later wiring item imports this module from chair_exec; a runtime import would cycle
    from agent_tools.chair_exec import Result

__all__ = ["ForgePort", "GitPort", "StorePort", "perform_carry"]


class GitPort(Protocol):
    def fetch_branch(self, host: str, branch: str) -> None: ...

    def create_branch_from_main(self, name: str) -> None: ...

    def cherry_pick(self, commit: str) -> list[str]:
        """The conflicting files, empty on success; on conflict the port has already aborted the pick."""
        ...

    def push(self, branch: str) -> None: ...

    def run_checks(self) -> list[str]:
        """Runs the repository's `.agent-checks` commands; the names of the failing ones."""
        ...

    def already_on_main(self, commit: str) -> bool: ...


class ForgePort(Protocol):
    def open_pr(self, branch: str, title: str, body: str) -> str: ...

    def wait_checks(self, pr: str) -> list[str]:
        """The names of the failing checks."""
        ...

    def merge(self, pr: str) -> None: ...


class StorePort(Protocol):
    def mark_landed(self, task: str, run: str) -> None: ...

    def set_done(self, task: str) -> None: ...


def _stopped(stop: Action) -> Result:
    # A stop is reported as the planner's own needs_chair, the same shape chair_exec's `_escalate` returns.
    return {"action": stop, "status": "escalated", "reason": stop.get("reason", "")}


def _pr_text(action: Action) -> tuple[str, str]:
    """Title and body for the carry PR; the body ends with the footer the land path adds."""
    title = f"Carry {action['initiative']} {action['phase']}"
    tasks = [f"- {pick['task']} ({pick['commit']})" for pick in action["picks"]]
    return title, "\n".join([title, "", *tasks, "", pr_footer(None)])


def _finish(action: Action, store: StorePort) -> Result:
    for pick in action["picks"]:
        store.mark_landed(pick["task"], pick["run"])  # an already-on-main pick is marked with its own run
        store.set_done(pick["task"])
    return {"action": action, "status": "done", "reason": ""}


def perform_carry(action: Action, git: GitPort, forge: ForgePort, store: StorePort) -> Result:
    initiative, phase, picks = action["initiative"], action["phase"], action["picks"]
    for host, branch in dict.fromkeys((p["host"], p["branch"]) for p in picks):
        git.fetch_branch(host, branch)
    git.create_branch_from_main(action["pr_branch"])
    picked = 0
    for pick in picks:
        if git.already_on_main(pick["commit"]):
            continue
        conflicts = git.cherry_pick(pick["commit"])
        if conflicts:
            return _stopped(chair_carry.conflict_stop(initiative, phase, pick["task"], conflicts))
        picked += 1
    if not picked:
        return _finish(action, store)
    failing = git.run_checks()
    if failing:
        return _stopped(chair_carry.checks_failed_stop(initiative, phase, failing))
    git.push(action["pr_branch"])
    title, body = _pr_text(action)
    pr = forge.open_pr(action["pr_branch"], title, body)
    failing = forge.wait_checks(pr)
    if failing:
        return _stopped(chair_carry.checks_failed_stop(initiative, phase, failing))
    forge.merge(pr)
    return _finish(action, store)
