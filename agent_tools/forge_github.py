"""The github forge: `gh` and `git push`. The protocol is in `agent_tools.forge`."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from agent_tools import land
from agent_tools.forge import ForgeError


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


def push_argv(repo: Path, branch: str, expected_tip: str | None = None) -> list[str]:
    """The push argv; with `expected_tip` it leases the branch, so only that remote tip may be replaced."""
    lease = [f"--force-with-lease={branch}:{expected_tip}"] if expected_tip else []
    return ["git", "-C", str(repo), "push", *lease, "-u", "origin", branch]


def push(repo: Path, branch: str, expected_tip: str | None = None) -> tuple[bool, str]:
    r = subprocess.run(push_argv(repo, branch, expected_tip), capture_output=True, text=True)
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


def parse_merge_state(stdout: str) -> str:
    """The `mergeStateStatus` of `gh pr view --json mergeStateStatus` output, unchanged; ForgeError when absent."""
    data = _loads(stdout)
    state = data.get("mergeStateStatus") if isinstance(data, dict) else None
    if not isinstance(state, str) or not state:
        raise ForgeError(f"could not read mergeStateStatus: {stdout.strip()}")
    return state


def merge_state_argv(pr: int) -> list[str]:
    return ["gh", "pr", "view", str(pr), "--json", "mergeStateStatus"]


def update_branch_argv(pr: int) -> list[str]:
    return ["gh", "pr", "update-branch", str(pr)]


def _gh_run(argv: list[str], cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    """`argv` run through gh in `cwd` (the repository, so gh finds its GitHub remote); ForgeError with gh's stderr
    when it cannot run or exits nonzero."""
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise ForgeError(f"{' '.join(argv)}: {exc}") from exc
    if r.returncode != 0:
        raise ForgeError((r.stderr or r.stdout or "").strip() or f"{' '.join(argv)} exited {r.returncode}")
    return r


def merge_state(pr: int, *, repo: Path | str | None = None) -> str:
    return parse_merge_state(_gh_run(merge_state_argv(pr), repo).stdout or "")


def update_branch(pr: int, *, repo: Path | str | None = None) -> None:
    _gh_run(update_branch_argv(pr), repo)


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


def _loads(stdout: str):
    try:
        return json.loads(stdout)
    except (ValueError, TypeError):
        return None


def parse_base_ref(stdout: str) -> str | None:
    """The `baseRefName` of `gh pr view --json baseRefName` output, or None."""
    data = _loads(stdout)
    base = data.get("baseRefName") if isinstance(data, dict) else None
    return base if isinstance(base, str) and base else None


def parse_ruleset_contexts(stdout: str) -> tuple[str, ...] | None:
    """Context names of every `required_status_checks` rule in a `rules/branches/{base}` body; None when unparseable, () when none require any."""
    data = _loads(stdout)
    if not isinstance(data, list):
        return None
    try:
        names = [c["context"] for rule in data if isinstance(rule, dict) and rule.get("type") == "required_status_checks"
                 for c in rule["parameters"]["required_status_checks"]]
        return tuple(dict.fromkeys(names))
    except (KeyError, TypeError):
        return None


def parse_protection_contexts(stdout: str) -> tuple[str, ...] | None:
    """`contexts` and `checks[].context` of a classic `required_status_checks` body; None when unparseable."""
    data = _loads(stdout)
    if not isinstance(data, dict):
        return None
    contexts, checks = data.get("contexts") or [], data.get("checks") or []
    if not isinstance(contexts, list) or not isinstance(checks, list):
        return None
    try:
        return tuple(dict.fromkeys([*contexts, *(c["context"] for c in checks)]))
    except (KeyError, TypeError):
        return None


def rerun_run_ids(check_runs: dict) -> tuple[str, ...]:
    """Workflow run ids, in order and deduplicated, from the `details_url` of each failed check run. A failure with no `/actions/runs/<id>` url yields none."""
    urls = [r.get("details_url") or "" for r in check_runs.get("check_runs") or [] if r.get("conclusion") in land._FAILED_CONCLUSIONS]
    return tuple(dict.fromkeys(m[1] for m in (re.search(r"/actions/runs/(\d+)", u) for u in urls) if m))


def failed_check_run_ids(check_runs: dict) -> frozenset:
    """The `id` of each failed check run. A rerun replaces these, so a failure made only of them is the one already rerun."""
    return frozenset(r.get("id") for r in check_runs.get("check_runs") or [] if r.get("conclusion") in land._FAILED_CONCLUSIONS)


def base_ref_argv(ref: str) -> list[str]:
    """`gh pr view` for `ref`'s PR. `HEAD` is no branch name, so it is left out and gh reads the checked-out branch."""
    return ["gh", "pr", "view", *([] if ref == "HEAD" else [ref]), "--json", "baseRefName"]


def _gh_text(argv: list[str], repo: Path) -> str | None:
    """Stdout of `argv`, or None when it cannot run or exits nonzero."""
    try:
        r = subprocess.run(argv, cwd=repo, capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout or "" if r.returncode == 0 else None


def _read_required(repo: Path, ref: str) -> tuple[str, ...] | None:
    """Required check names for the base of `ref`'s PR: the rulesets', else classic branch protection's. () means both were read and require nothing; None means a call failed or a body was unreadable."""
    base = parse_base_ref(_gh_text(base_ref_argv(ref), repo) or "")
    if base is None:
        return None
    root = "repos/{owner}/{repo}"
    ruleset = parse_ruleset_contexts(_gh_text(["gh", "api", f"{root}/rules/branches/{base}"], repo) or "")
    if ruleset != ():  # names, or None for an unreadable ruleset: its contexts are unknown, so protection alone must not stand in
        return ruleset
    return parse_protection_contexts(_gh_text(["gh", "api", f"{root}/branches/{base}/protection/required_status_checks"], repo) or "")


def wait_checks(repo: Path, timeout_s: float, sleep=time.sleep, now=time.monotonic, *, ref: str = "HEAD") -> tuple[bool, str]:
    required = _read_required(repo, ref)
    # Edge bend (A2): a count of consecutive unreadable polls, reset by any readable one.
    errors = 0
    # Edge bend (A2): `rerun_of` holds the failed check run ids once gh accepted every rerun, and a newer failure is final.
    # `rerun_tries` counts refused rerun calls, which gh returns while a sibling job still runs.
    # `stale_polls` counts polls that still show only the rerun failures. Both stop at POLL_ERROR_LIMIT.
    # `accepted` holds the run ids gh took a rerun for, so a retry after a partial refusal never reruns one twice.
    rerun_of: frozenset | None = None
    accepted: frozenset = frozenset()
    rerun_tries = stale_polls = 0

    def rerun(ids: tuple[str, ...], failed: frozenset) -> tuple[int, str]:
        nonlocal rerun_of, rerun_tries, accepted
        todo = [i for i in ids if i not in accepted]
        refused = [i for i in todo if _gh_text(["gh", "run", "rerun", i, "--failed"], repo) is None]
        accepted = accepted | frozenset(todo) - frozenset(refused)
        rerun_tries += bool(refused)
        rerun_of = None if refused else failed
        return land.PENDING_RC, (f"rerun refused for runs {', '.join(refused)}, retrying ({rerun_tries}/{land.POLL_ERROR_LIMIT})"
                                 if refused else f"failed runs rerun once: {', '.join(ids)}")

    def poll() -> tuple[int, str]:
        nonlocal errors, stale_polls
        ok, value = _read_checks(repo, ref)
        errors = 0 if ok else errors + 1
        if ok:
            result = land.check_poll_result(*value, required)
            failed = failed_check_run_ids(value[0])
            if result[0] == 0 or not failed:
                return result
            if rerun_of is not None:
                stale = failed <= rerun_of and stale_polls < land.POLL_ERROR_LIMIT
                stale_polls += stale
                return (land.PENDING_RC, "rerun not yet registered: " + result[1]) if stale else result
            ids = rerun_run_ids(value[0])
            return rerun(ids, failed) if ids and rerun_tries < land.POLL_ERROR_LIMIT else result
        result = land.unreadable_poll(errors, value)
        if land.is_pending(result[0]):
            sleep(land.poll_backoff_s(errors))  # on top of the 15s between polls: a rate limit needs room
        return result
    return land.await_checks(poll, timeout_s, sleep, now)
