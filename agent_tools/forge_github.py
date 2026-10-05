"""The github forge: `gh` and `git push`. The protocol is in `agent_tools.forge`."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from agent_tools import land


def find_open_prs(repo: Path, branch: str) -> list[int] | str:
    """Numbers of the open PRs whose head is `branch`, or the reason they
    could not be listed."""
    try:
        r = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number"],
                           cwd=repo, capture_output=True, text=True)
    except OSError as exc:
        return f"could not list open pull requests for {branch}: {exc}"
    if r.returncode != 0:
        return f"could not list open pull requests for {branch}: {(r.stderr or r.stdout).strip()}"
    try:
        return [int(p["number"]) for p in json.loads(r.stdout or "[]")]
    except (ValueError, KeyError, TypeError):
        return f"could not read the open pull requests for {branch}: {r.stdout.strip()}"


UNKNOWN_PR_STATE = {"state": "unknown", "merged_at": None}
_PR_STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


def parse_pr_state(stdout: str) -> dict:
    """`gh pr view --json state,mergedAt` output as `{"state", "merged_at"}`; `merged_at` is gh's UTC string, set only when merged."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return dict(UNKNOWN_PR_STATE)
    raw = data.get("state") if isinstance(data, dict) else None
    state = _PR_STATES.get(raw) if isinstance(raw, str) else None
    if state is None:
        return dict(UNKNOWN_PR_STATE)
    merged_at = data.get("mergedAt")
    return {"state": state, "merged_at": merged_at if state == "merged" and isinstance(merged_at, str) else None}


def pr_state(url: str) -> dict:
    try:
        r = subprocess.run(["gh", "pr", "view", url, "--json", "state,mergedAt"], capture_output=True, text=True)
    except OSError:
        return dict(UNKNOWN_PR_STATE)
    return parse_pr_state(r.stdout) if r.returncode == 0 else dict(UNKNOWN_PR_STATE)


def push(repo: Path, branch: str) -> tuple[bool, str]:
    r = subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", branch], capture_output=True, text=True)
    return r.returncode == 0, (branch if r.returncode == 0 else r.stderr.strip() or r.stdout.strip())


def open_pr(repo: Path, title: str, body: str, *, head: str | None = None, base: str | None = None) -> tuple[bool, str]:
    refs = [*(["--head", head] if head else []), *(["--base", base] if base else [])]
    r = subprocess.run(["gh", "pr", "create", "--title", title, "--body", body, *refs], cwd=repo, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout.strip() or r.stderr.strip())


def _update_local_default(repo: Path, default: str) -> str:
    """Bring the local `default` branch up to `origin` without a checkout; git's output, or the failure."""
    current = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], capture_output=True, text=True)
    on_default = current.returncode == 0 and current.stdout.strip() == default
    argv = ["pull", "--ff-only", "origin", default] if on_default else ["fetch", "origin", f"{default}:{default}"]
    r = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True)
    out = r.stdout.strip() or r.stderr.strip()
    return out if r.returncode == 0 else f"local {default} not updated: {out}"


def merge(repo: Path, step: dict) -> tuple[bool, str]:
    """Merge the PR of `step["branch"]`, then update the local default branch.

    Once gh succeeds the PR has merged, so a failed local update is reported in
    the detail and the result stays ok.
    """
    r = subprocess.run(["gh", "pr", "merge", step["branch"], "--squash", "--delete-branch"], cwd=repo, capture_output=True, text=True)
    merged = r.stdout.strip() or r.stderr.strip()
    if r.returncode != 0:
        return False, merged
    return True, "\n".join(filter(None, [merged, _update_local_default(repo, step["default_branch"])]))


def _read_checks(repo: Path, ref: str = "HEAD"):
    """`(True, (check_runs, status))` for `ref`'s REST check bodies, or `(False, detail)` on a failed or unparseable call."""
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True)
    if head.returncode != 0:
        return False, head.stderr.strip() or f"git rev-parse {ref} failed"
    argvs = land.rest_checks_argvs(head.stdout.strip())
    bodies = []
    for argv, key in zip(argvs, ("check_runs", "statuses")):
        r = subprocess.run(argv, cwd=repo, capture_output=True, text=True)
        body = land.merge_pages(r.stdout or "", key) if r.returncode == 0 else None
        if body is None:
            return False, (r.stderr or r.stdout or "").strip() or f"unreadable output from {argv[-1]}"
        bodies.append(body)
    return True, tuple(bodies)


def wait_checks(repo: Path, timeout_s: float, sleep=time.sleep, now=time.monotonic, *, ref: str = "HEAD") -> tuple[bool, str]:
    # Edge bend (A2): a count of consecutive unreadable polls, reset by any readable one.
    errors = 0

    def poll() -> tuple[int, str]:
        nonlocal errors
        ok, value = _read_checks(repo, ref)
        errors = 0 if ok else errors + 1
        if ok:
            return land.check_poll_result(*value)
        result = land.unreadable_poll(errors, value)
        if land.is_pending(result[0]):
            sleep(land.poll_backoff_s(errors))  # on top of the 15s between polls: a rate limit needs room
        return result
    return land.await_checks(poll, timeout_s, sleep, now)
