"""Real git and gh adapters for the revert executor, and the main CI reader.

Every adapter takes a runner, a callable from an argv list to (exit code, output).
Nothing here spawns a process; the caller injects the runner that does.
"""

from __future__ import annotations

import json
from collections.abc import Callable

__all__ = [
    "CI_WATCH_TIMEOUT_SECONDS",
    "LOG_TAIL_LINES",
    "GhForgePort",
    "GitRevertPort",
    "Runner",
    "bounded",
    "main_ci",
    "parent_count",
]

Runner = Callable[[list[str]], tuple[int, str]]

LOG_TAIL_LINES = 40
# The longest a revert port waits on any one command, `gh pr checks --watch` being the slow one.
CI_WATCH_TIMEOUT_SECONDS = 30 * 60
_PENDING_STATUSES = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})


def parent_count(rev_list_line: str) -> int:
    """Parents named on a `git rev-list --parents -n 1` line, whose first hash is the commit itself."""
    return max(len(rev_list_line.split()) - 1, 0)


def bounded(run: Callable[..., tuple[int, str]], timeout: int = CI_WATCH_TIMEOUT_SECONDS) -> Runner:
    """A Runner that gives every call `timeout`. `run` reports expiry as a non-zero exit whose output names the bound."""
    return lambda argv: run(argv, timeout=timeout)


def _lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def _checked(runner: Runner, argv: list[str]) -> str:
    code, output = runner(argv)
    if code != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {code}: {output.strip()}")
    return output


class GitRevertPort:
    def __init__(self, runner: Runner) -> None:
        self._run = runner

    def fetch_main(self, repo: str) -> None:
        _checked(self._run, ["git", "-C", repo, "fetch", "origin", "main"])

    def revert_onto_branch(self, repo: str, branch: str, commit: str) -> list[str]:
        git = ["git", "-C", repo]
        parents = parent_count(_checked(self._run, [*git, "rev-list", "--parents", "-n", "1", commit]))
        _checked(self._run, [*git, "checkout", "-b", branch, "origin/main"])
        merge_flags = ["-m", "1"] if parents == 2 else []
        code, output = self._run([*git, "revert", "--no-edit", *merge_flags, commit])
        if code == 0:
            return []
        _, unmerged = self._run([*git, "diff", "--name-only", "--diff-filter=U"])
        self._run([*git, "revert", "--abort"])
        # Drop the branch so a retry's `checkout -b` does not collide with it.
        self._run([*git, "checkout", "--detach", "origin/main"])
        self._run([*git, "branch", "-D", branch])
        files = _lines(unmerged)
        if not files:
            # A failed revert with no unmerged files is not a conflict; [] would read as a clean revert.
            raise RuntimeError(f"git revert {commit} exited {code} with no conflicts: {output.strip()}")
        return files

    def push(self, repo: str, branch: str) -> None:
        _checked(self._run, ["git", "-C", repo, "push", "origin", branch])


class GhForgePort:
    def __init__(self, runner: Runner) -> None:
        self._run = runner

    def open_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        output = _checked(
            self._run,
            ["gh", "pr", "create", "--repo", repo, "--head", branch, "--title", title, "--body", body],
        )
        urls = [line for line in _lines(output) if line.startswith("http")]
        if not urls:
            raise RuntimeError(f"gh pr create printed no url: {output.strip()}")
        return urls[-1]

    def wait_checks(self, pr: str) -> list[str]:
        # Non-zero exit is how gh reports failing or pending checks, so the output is read regardless.
        # Piped, gh prints one tab-separated row per check: name, state, elapsed, url.
        # An empty list means green and the executor merges on it, so no rows or an unexplained exit raises.
        code, output = self._run(["gh", "pr", "checks", pr, "--watch"])
        rows = [line.split("\t") for line in output.splitlines() if "\t" in line]
        failing = [row[0].strip() for row in rows if len(row) > 1 and row[1].strip() == "fail"]
        if not rows or (code != 0 and not failing):
            raise RuntimeError(f"gh pr checks {pr} exited {code} with no failing check: {output.strip()}")
        return failing

    def merge(self, pr: str) -> None:
        _checked(self._run, ["gh", "pr", "merge", pr, "--squash"])


def main_ci(runner: Runner, repo: str, commit: str) -> tuple[str, str]:
    """State of main's first workflow run for the commit: pending, green or red, with the failing log tail."""
    listed = _checked(
        runner,
        [
            "gh", "run", "list", "--repo", repo, "--branch", "main", "--commit", commit,
            "--json", "databaseId,status,conclusion", "--limit", "1",
        ],
    )  # fmt: skip
    runs = json.loads(listed or "[]")
    if not runs:
        return "pending", ""
    run = runs[0]
    if run.get("status") in _PENDING_STATUSES:
        return "pending", ""
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "green", ""
    if conclusion != "failure":
        # Cancelled, skipped, neutral and the rest are not evidence that main is red.
        return "pending", f"run concluded {conclusion}"
    code, log = runner(["gh", "run", "view", str(run["databaseId"]), "--repo", repo, "--log-failed"])
    tail = "\n".join(log.splitlines()[-LOG_TAIL_LINES:])
    return "red", tail if code == 0 else f"failing log unavailable, gh run view exited {code}: {tail}"
