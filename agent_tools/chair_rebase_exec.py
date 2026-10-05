"""Perform a rebase_phase: back up the old tip, then recreate the phase branch from its base."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agent_tools.chair_rebase import backup_ref
from agent_tools.chair_types import Action

if TYPE_CHECKING:
    from agent_tools.chair_exec import Result


class RebasePort(Protocol):
    def branch_tip(self, branch: str) -> str | None:
        """The pushed tip on the remote, so a recreate whose push failed still reads as the old tip."""
        ...

    def create_ref(self, ref: str, sha: str) -> None: ...

    def recreate_branch(self, branch: str, base: str) -> None:
        """Force-point the branch at base and push it; raises if either step fails."""
        ...

    def ref_exists(self, ref: str) -> bool: ...


def _moved_stop(action: Action, tip: str) -> Action:
    initiative, phase, branch = action["initiative"], action["phase"], action["branch"]
    return {
        "kind": "needs_chair", "initiative": initiative, "phase": phase, "cause": "rebase_tip_moved",
        "epoch": action.get("epoch", 0),
        "reason": f"initiative {initiative} phase {phase}: {branch} is at {tip[:8]}, "
        f"not the planned {action['tip'][:8]}; nothing was changed",
    }


def _moved(action: Action, tip: str) -> Result:
    reason = f"branch {action['branch']} moved since planning"
    return {"action": action, "status": "refused", "reason": reason, "needs_chair": _moved_stop(action, tip)}


def perform_rebase(action: Action, port: RebasePort) -> Result:
    """Recreates only after the backup ref is confirmed; at base with that backup present is a no-op."""
    branch, base = action["branch"], action["base"]
    ref = backup_ref(branch, action["tip"])
    try:
        tip = port.branch_tip(branch)
        if tip is None:
            return {"action": action, "status": "skipped", "reason": f"branch {branch} is gone"}
        if tip in (base, port.branch_tip(base)):
            # At base with no backup of the planned tip means something else moved it there.
            return (
                {"action": action, "status": "skipped", "reason": f"branch {branch} is already at {base}"}
                if port.ref_exists(ref)
                else _moved(action, tip)
            )
        if tip != action["tip"]:
            return _moved(action, tip)
        if not port.ref_exists(ref):
            port.create_ref(ref, tip)
        if not port.ref_exists(ref):
            return {"action": action, "status": "failed", "reason": f"backup ref {ref} could not be confirmed; {branch} was not recreated"}
        port.recreate_branch(branch, base)
    except Exception as exc:  # the edge: a port failure becomes a failed result, and a rerun retries from the remote tip
        return {"action": action, "status": "failed", "reason": f"rebase of {branch} failed: {exc}"}
    return {"action": action, "status": "done", "reason": f"{branch} backed up as {ref} and recreated from {base}"}
