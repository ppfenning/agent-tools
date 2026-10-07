"""The forgejo forge: the Forgejo REST API through `agent_tools.forgejo_api`, and `git push`. The protocol is in `agent_tools.forge`.

The protocol functions take only `repo`, so the server and the project come from the repo's `origin`, which must be an
http(s) URL, and the token from the `FORGEJO_TOKEN` variable. The profile's `forgejo_*` keys do not reach this module.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from agent_tools import land
from agent_tools.forge import ForgeError
from agent_tools.forge_github import _update_local_default, push
from agent_tools.forgejo_api import (
    TOKEN_ENV_KEY,
    URL_KEY,
    ForgejoError,
    ForgejoSettings,
    read_settings,
    request,
)

__all__ = ["find_open_prs", "merge", "merge_state", "open_pr", "push", "update_branch", "wait_checks"]

DEFAULT_TOKEN_ENV = "FORGEJO_TOKEN"
# What forge_github's merge_state says for a PR that can and cannot merge; Forgejo has no finer status.
MERGEABLE, CONFLICTED, UNSETTLED = "CLEAN", "DIRTY", "UNKNOWN"
PAGE_SIZE = 50
# A guard on the page loop: a server that ignores `page` would otherwise repeat page one forever.
MAX_PAGES = 20


@dataclass(frozen=True)
class Target:
    settings: ForgejoSettings
    owner: str
    name: str

    def path(self, tail: str) -> str:
        return f"/api/v1/repos/{quote(self.owner, safe='')}/{quote(self.name, safe='')}{tail}"


def _message(status: int, body: object) -> str:
    text = body.get("message") if isinstance(body, dict) else None
    return text if isinstance(text, str) and text else f"HTTP {status}"


def parse_remote(url: str) -> tuple[str, str, str] | None:
    """`(base_url, owner, name)` of an http(s) remote URL, or None for any other kind. A path prefix stays in the base."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if parts.scheme not in ("http", "https") or not parts.hostname or len(segments) < 2:
        return None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    prefix, owner, name = segments[:-2], segments[-2], segments[-1].removesuffix(".git")
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}" + "".join(f"/{s}" for s in prefix), owner, name


def parse_open_prs(status: int, body: object, branch: str) -> list[int] | str:
    """Numbers of the open PRs whose head is `branch` in a pull list body, or the reason it could not be read."""
    if status != 200 or not isinstance(body, list):
        return f"could not list open pull requests for {branch}: {_message(status, body)}"
    try:
        return [int(p["number"]) for p in body if isinstance(p, dict) and (p.get("head") or {}).get("ref") == branch]
    except (KeyError, TypeError, ValueError):
        return f"could not read the open pull requests for {branch}"


def parse_open_pr(status: int, body: object) -> tuple[bool, str]:
    """`(True, html_url)` for a created pull request, else `(False, Forgejo's message)`."""
    url = body.get("html_url") if isinstance(body, dict) else None
    if status in (200, 201) and isinstance(url, str) and url:
        return True, url
    return False, _message(status, body)


def parse_checks(body: object) -> tuple[int, str]:
    """`(returncode, output)` as `land.check_poll_result` gives: 0 pass, `land.PENDING_RC` pending, 1 fail.

    A commit with no statuses is `(1, "no checks reported")`, which `land.wait_decision` retries until its timeout.
    """
    if not isinstance(body, dict):
        raise ForgeError("could not read the commit status")
    statuses = [s for s in body.get("statuses") or [] if isinstance(s, dict)]
    if not statuses:
        return 1, land._NO_CHECKS
    state = body.get("state")
    if state == "success":
        return 0, ""
    if state in ("failure", "error"):
        return 1, f"failing checks: {_context_names(statuses, ('failure', 'error'))}"
    if state == "pending":
        return land.PENDING_RC, f"checks pending: {_context_names(statuses, ('pending',))}"
    return 1, f"unrecognised combined status: {state}"


def _context_names(statuses: list[dict], states: tuple[str, ...]) -> str:
    """Contexts of the statuses in `states`; `combined status` when none is, as when the server's rollup disagrees with its rows."""
    return ", ".join(s.get("context") or "status" for s in statuses if s.get("state") in states) or "combined status"


def parse_merge_state(body: object) -> str:
    """`CLEAN` or `DIRTY` from an open pull request's `mergeable`; `UNKNOWN` for a closed or merged one, whose `mergeable` is false
    without a conflict. ForgeError when `mergeable` is absent or not a bool."""
    flag = body.get("mergeable") if isinstance(body, dict) else None
    if not isinstance(flag, bool):
        raise ForgeError("could not read mergeable")
    if body.get("merged") is True or body.get("state", "open") != "open":
        return UNSETTLED
    return MERGEABLE if flag else CONFLICTED


def parse_merge_response(status: int, body: object) -> tuple[bool, str]:
    """`(True, "merged")` on 2xx, `(False, Forgejo's message)` on 405 and 409; any other status raises ForgeError."""
    if 200 <= status < 300:
        return True, "merged"
    if status in (405, 409):
        return False, _message(status, body)
    raise ForgeError(f"merge failed: {_message(status, body)}")


def parse_delete_response(status: int, body: object) -> tuple[bool, str]:
    """`(True, "deleted")` on 200 or 204, else `(False, Forgejo's message)`."""
    return (True, "deleted") if status in (200, 204) else (False, f"branch not deleted: {_message(status, body)}")


def _call(target: Target, env: Mapping[str, str], method: str, tail: str, body: object | None = None) -> tuple[int, object | None]:
    try:
        return request(target.settings, env, method, target.path(tail), body=body)
    except ForgejoError as exc:
        raise ForgeError(str(exc)) from exc


def list_open_prs(target: Target, env: Mapping[str, str], branch: str, page: int = 1) -> list[int] | str:
    """Every page of open pulls, until one comes back short; the list has no head filter, so matching is client-side."""
    try:
        status, body = _call(target, env, "GET", f"/pulls?state=open&limit={PAGE_SIZE}&page={page}")
    except ForgeError as exc:
        return f"could not list open pull requests for {branch}: {exc}"
    found = parse_open_prs(status, body, branch)
    if isinstance(found, str) or len(body) < PAGE_SIZE or page >= MAX_PAGES:
        return found
    rest = list_open_prs(target, env, branch, page + 1)
    return rest if isinstance(rest, str) else [*found, *rest]


def create_pr(target: Target, env: Mapping[str, str], title: str, body: str, head: str, base: str) -> tuple[bool, str]:
    return parse_open_pr(*_call(target, env, "POST", "/pulls", {"title": title, "body": body, "head": head, "base": base}))


def read_checks(target: Target, env: Mapping[str, str], sha: str) -> tuple[int, str]:
    status, body = _call(target, env, "GET", f"/commits/{quote(sha, safe='')}/status")
    if status != 200:
        raise ForgeError(f"could not read the status of {sha}: {_message(status, body)}")
    return parse_checks(body)


def read_merge_state(target: Target, env: Mapping[str, str], pr: int) -> str:
    status, body = _call(target, env, "GET", f"/pulls/{pr}")
    if status != 200:
        raise ForgeError(f"could not read pull request {pr}: {_message(status, body)}")
    return parse_merge_state(body)


def squash_merge(target: Target, env: Mapping[str, str], pr: int) -> tuple[bool, str]:
    return parse_merge_response(*_call(target, env, "POST", f"/pulls/{pr}/merge", {"Do": "squash"}))


def delete_branch(target: Target, env: Mapping[str, str], branch: str) -> tuple[bool, str]:
    return parse_delete_response(*_call(target, env, "DELETE", f"/branches/{quote(branch, safe='/')}"))


def _git(repo: Path | str, *argv: str) -> str:
    """Stdout of `git -C repo argv`; ForgeError with git's stderr when it cannot run or exits nonzero."""
    try:
        r = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True)
    except OSError as exc:
        raise ForgeError(f"git {' '.join(argv)}: {exc}") from exc
    if r.returncode != 0:
        raise ForgeError(r.stderr.strip() or f"git {' '.join(argv)} exited {r.returncode}")
    return r.stdout.strip()


def _target(repo: Path | str) -> Target:
    """The Forgejo project `repo`'s `origin` names; ForgeError when it has no origin or origin is not an http(s) URL."""
    url = _git(repo, "remote", "get-url", "origin")
    parsed = parse_remote(url)
    if parsed is None:
        raise ForgeError(f"origin {url!r} is not an http(s) Forgejo URL")
    base, owner, name = parsed
    try:
        return Target(read_settings({URL_KEY: base, TOKEN_ENV_KEY: DEFAULT_TOKEN_ENV}), owner, name)
    except ForgejoError as exc:
        raise ForgeError(str(exc)) from exc


def find_open_prs(repo: Path, branch: str) -> list[int] | str:
    """Numbers of the open PRs whose head is `branch`, or the reason they could not be listed."""
    try:
        return list_open_prs(_target(repo), os.environ, branch)
    except ForgeError as exc:
        return f"could not list open pull requests for {branch}: {exc}"


def open_pr(repo: Path, title: str, body: str, *, head: str | None = None, base: str | None = None) -> tuple[bool, str]:
    """`base` None is the branch `origin/HEAD` names, so it is unset for a repo that was not cloned."""
    try:
        source = head or _git(repo, "symbolic-ref", "--short", "HEAD")
        into = base or _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/")
        return create_pr(_target(repo), os.environ, title, body, source, into)
    except ForgeError as exc:
        return False, str(exc)


def merge_state(pr: int, *, repo: Path | str | None = None) -> str:
    if repo is None:
        raise ForgeError("forgejo: a PR number alone does not name a repository")
    return read_merge_state(_target(repo), os.environ, pr)


def merge(repo: Path, step: dict) -> tuple[bool, str]:
    """Squash-merge the PR of `step["branch"]`, delete its branch, then update the local default branch.

    Once Forgejo merges, the PR has merged, so a failed delete or local update is reported in the detail and the result stays ok.
    """
    try:
        target = _target(repo)
        found = list_open_prs(target, os.environ, step["branch"])
        if isinstance(found, str):
            return False, found
        if not found:
            return False, f"no open pull request for {step['branch']}"
        merged, detail = squash_merge(target, os.environ, found[0])
    except ForgeError as exc:
        return False, str(exc)
    if not merged:
        return False, detail
    try:
        deleted = delete_branch(target, os.environ, step["branch"])[1]
    except ForgeError as exc:
        deleted = f"branch not deleted: {exc}"
    left = _drop_local_branch(repo, step["branch"], step["default_branch"])
    return True, "\n".join(filter(None, [detail, deleted, left, _update_local_default(repo, step["default_branch"])]))


def _drop_local_branch(repo: Path | str, branch: str, default: str) -> str:
    """Leave `branch` for `default` when it is checked out, then delete it, as `gh pr merge --delete-branch` does."""
    try:
        on_branch = _git(repo, "symbolic-ref", "--short", "HEAD") == branch
    except ForgeError:
        on_branch = False
    try:
        switched = _git(repo, "checkout", "-q", default) if on_branch else ""
    except ForgeError as exc:
        return f"local {branch} kept: {exc}"
    try:
        _git(repo, "branch", "-D", branch)
    except ForgeError:
        return switched
    return f"deleted local {branch}"


def update_branch(pr: int, *, repo: Path | str | None = None) -> None:
    """Merge the base branch into PR `pr`'s head; ForgeError with Forgejo's message on failure."""
    if repo is None:
        raise ForgeError("forgejo: a PR number alone does not name a repository")
    status, body = _call(_target(repo), os.environ, "POST", f"/pulls/{pr}/update")
    if not 200 <= status < 300:
        raise ForgeError(f"could not update pull request {pr}: {_message(status, body)}")


def wait_checks(repo: Path, timeout_s: float, sleep=time.sleep, now=time.monotonic, *, ref: str = "HEAD") -> tuple[bool, str]:
    try:
        target, sha = _target(repo), _git(repo, "rev-parse", ref)
    except ForgeError as exc:
        return False, str(exc)
    # Edge bend (A2): a count of consecutive unreadable polls, reset by any readable one.
    errors = 0

    def poll() -> tuple[int, str]:
        nonlocal errors
        try:
            result = read_checks(target, os.environ, sha)
        except ForgeError as exc:
            errors += 1
            unreadable = land.unreadable_poll(errors, str(exc))
            if land.is_pending(unreadable[0]):
                sleep(land.poll_backoff_s(errors))
            return unreadable
        errors = 0
        return result
    return land.await_checks(poll, timeout_s, sleep, now)
