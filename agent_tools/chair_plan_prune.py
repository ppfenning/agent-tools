"""Select the previous run's phase worktrees to prune before a relaunch."""

_HEADS = "refs/heads/"


def _entry(block: str) -> dict:
    """Path and branch of one porcelain block; the branch is empty when detached or bare."""
    lines = block.splitlines()
    path = next((ln[len("worktree "):] for ln in lines if ln.startswith("worktree ")), "")
    ref = next((ln[len("branch "):] for ln in lines if ln.startswith("branch ")), "")
    return {"path": path, "branch": ref[len(_HEADS):] if ref.startswith(_HEADS) else ""}


def worktrees_to_prune(porcelain: str, initiative: str) -> list[dict]:
    """Worktrees on epic/<initiative>/<phase>, in porcelain order; git lists the main checkout first, so it is skipped."""
    blocks = [b for b in porcelain.strip().split("\n\n") if b.strip()]
    prefix = f"epic/{initiative}/"
    return [e for e in map(_entry, blocks[1:]) if e["path"] and e["branch"].startswith(prefix)]


def phases_to_carry(approved: list[dict], initiative: str) -> set[str]:
    """Phases of the initiative's approved rows that are not yet phase_done: partial work a prune must not delete."""
    return {r["phase"] for r in approved if r["initiative"] == initiative and not r["phase_done"]}


def prune_argv(entries: list[dict], carry: set[str] = frozenset()) -> list[list[str]]:
    """Every worktree remove first, then a branch delete for every entry not on a carried phase.

    git refuses to delete a branch a worktree holds, so removes go first regardless of carry.
    A carried phase's worktree is still removed so the relaunch can recreate it, but its branch
    survives the prune: that is the unlanded work being carried forward.
    """
    return [["git", "worktree", "remove", "--force", e["path"]] for e in entries] + [
        ["git", "branch", "-D", e["branch"]]
        for e in entries
        if e["branch"].rsplit("/", 1)[-1] not in carry
    ]
