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


def prune_argv(entries: list[dict]) -> list[list[str]]:
    """Every worktree remove first, then every branch delete: git refuses to delete a branch a worktree holds."""
    return [["git", "worktree", "remove", "--force", e["path"]] for e in entries] + [
        ["git", "branch", "-D", e["branch"]] for e in entries
    ]
