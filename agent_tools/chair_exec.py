"""The thin edge of a chair tick: perform planned actions through injected callables.

Pure helpers decide what an action means; `perform` only fences, dispatches and collects results.
Argv spellings follow `cox runs land --help` and `cox route launch epic|decompose|rescue --help`.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict

from agent_tools import (
    chair,
    chair_apply_fetch,
    chair_apply_review,
    chair_apply_widen,
    chair_housekeeping,
    chair_login_check,
    chair_login_watch,
    chair_plan_prune,
    courier,
    cox_settings,
    forge_auto,
    host_cmd,
    remote_fetch,
    remote_lane,
    route,
    run_store,
    stale_draft,
    store_cli,
)
from agent_tools.chair_carry_exec import ForgePort, GitPort, StorePort, perform_carry
from agent_tools.chair_facts import STRANDED_CAUSE
from agent_tools.chair_rebase_exec import RebasePort, perform_rebase
from agent_tools.chair_revert_exec import MERGED, RevertResult, perform_revert
from agent_tools.chair_revert_exec import ForgePort as RevertForgePort
from agent_tools.chair_revert_exec import GitPort as RevertGitPort
from agent_tools.chair_types import Action, LandTrigger, is_fenced
from agent_tools.land import approve_to_done
from agent_tools.remote_argv import LANE_HOST_TIMEOUT_S, ssh_argv, sync_argv

__all__ = [
    "LAUNCH_KINDS",
    "Deps",
    "LandSink",
    "Refusal",
    "Result",
    "argv_for",
    "branch_pattern",
    "decompose_id",
    "delete_branches_with",
    "edge_deps",
    "escalation",
    "land_commit",
    "land_refusal",
    "landed",
    "lane_writer",
    "perform",
    "smoke_targets",
    "tail",
]

Status = Literal[
    "fenced", "dry_run", "skipped", "refused", "recorded", "done", "failed", "landed", "not_landed", "escalated", "busy",
    "in_progress",
]

Run = Callable[[list[str]], tuple[int, str]]
Fetch = Callable[[list[str]], tuple[int, str] | None]  # None: the host was not reached, so its fact is left out
NOT_PROBED = "remote lanes not probed (dry run)"  # the line a dry run's status should carry for its remote-lane facts


class Result(TypedDict):
    action: Action
    status: Status
    reason: str
    run: NotRequired[str]  # a fetch_exit's run, for the next task's status line
    host: NotRequired[str]  # a fetch_exit's remote host, read from its <run>.remote.json
    needs_chair: NotRequired[Action]  # a refused land's classified needs_chair, escalated in place of the generic one;
    # or a clear_branches carry's conflicted-merge needs_chair, recorded alongside its own done result
    commit: NotRequired[str]  # a landed land's origin/main commit, from land_commit; set only when landed() was True


class LandSink(Protocol):
    """Where a tick hands a land and moves on; one land runs per repository, and its result comes back through `collect`."""

    def pending(self, repo: str) -> bool:
        """True while a land handed over for `repo` has not been collected."""
        ...

    def submit(self, action: Action, work: Callable[[], Result]) -> Result:
        """Start `work` in the background and return at once: `in_progress`, or `busy` when another process holds the repo."""
        ...

    def collect(self) -> list[Result]:
        """The lands that finished since the last call, each once."""
        ...


@dataclass(frozen=True)
class Refusal:
    reason: str


def _no_intake_id(path: str) -> str:
    return ""


@dataclass(frozen=True)
class Deps:
    run: Run  # (exit code, output); the only door to cox and git
    delete_branches: Callable[[str, str], tuple[list[str], str]]  # (repo, pattern) -> (deleted names, error text)
    acquire_lease: Callable[..., str]  # (holder, host), or (holder, host, steal=True) over an expired takeover -> refusal line or ""
    record: Callable[[Action], None]
    run_id: Callable[[Action], str]  # the run whose task a land applies to; "" when unknown
    repo_for: Callable[[Action], str]  # the repository a clear_branches acts in; "" when unknown
    ssh_for: Callable[[str], str] = lambda host: ""  # a lane host's name -> its ssh destination; "" when the hosts table has none
    intake_id: Callable[[str], str] = _no_intake_id  # an intake path -> the id in its frontmatter; "" when it has none
    runs_dir: Path = field(default_factory=lambda: Path("."))  # a fetch_exit's task records and remote record live here
    work_dir: Path = field(default_factory=lambda: Path("."))  # a fetch_exit's ticket files live under work_dir/work/<initiative>
    now: Callable[[], str] = lambda: datetime.now(UTC).isoformat()  # the tick's clock, passed to plan_stale_draft
    # The courier `to` of a stale_to_draft note. docs/design/courier.md's "Who writes" names "the
    # steward's proposals" as an existing writer to copy the recipient from, but no such call exists:
    # courier.send has exactly one call site before this patch (cli.py's generic `cox courier send`,
    # whose `to` comes from the caller's `--to` flag), and neither steward.py nor steward_draft.py
    # imports courier at all. "chair" reuses courier.inbox's own generic label instead (see its
    # docstring): it reaches whichever chair holds the lease, and bare `cox` prints that holder's
    # inbox to the human at start. A session label such as "chair-2026-09-27" would reach only that
    # one session, so the generic label is used here.
    note_to: str = "chair"
    check_login: Callable[[str], dict] | None = None  # a check_login's host name -> the hosts row cox host beat prints
    log_retention_days: int = 7  # the profile's log_retention_days: traces and run logs kept locally, in days
    harness_python: str = "python"  # the interpreter the trace prune runs under; the harness venv's python when one is configured
    send_signal: Callable[[int, int], None] = os.kill  # a stalled_usr1/stalled_stop's (pid, signal) call; the only door to os.kill
    ids_mode: str = "slug"  # the routing profile's `ids:` key; "sequence" makes a launch_decompose carry --initiative-id/--task-ids
    lands: LandSink | None = None  # None runs a land in line, as before; a sink runs it behind the tick
    carry_ports: Callable[[Action], tuple[GitPort, ForgePort, StorePort]] | None = None  # a carry_phase's ports; None refuses it
    rebase_port: Callable[[Action], RebasePort] | None = None  # a rebase_phase's port; None refuses it
    set_lanes: Callable[[str, int], int] | None = None  # a tune_lanes's (host, count) -> exit code; None refuses it
    revert_ports: Callable[[Action], tuple[RevertGitPort, RevertForgePort]] | None = None  # a revert_land's ports; None refuses it
    quarantine_phase: Callable[[str, str, str, str], None] | None = None  # (initiative, phase, cause, reason): quarantines that phase's tasks
    resolve_land: Callable[[str, str, str], None] | None = None  # (initiative, phase, outcome): the store's resolve write for a land


LAUNCH_KINDS = ("relaunch", "retry", "launch_epic", "launch_decompose", "rescue")
_UNFENCED = ("standby", "take_lease", "steer_clear")  # not writes, so a stale or missing epoch does not stop them
_REASON_CAP = 600
_GLOB_CHARS = frozenset("*?[]{}\\ \t")
# Every `cox runs land` refusal starts "land: refusing, ": a dirty repo, a branch conflict, a forge mismatch.
# Only the repo-lease line also says "<pid> on <host> is landing in <repo>", and only that one is worth retrying.
_REFUSAL_PREFIX = "land: refusing, "
_BUSY_MARK = " is landing in "


def _repo_busy(output: str) -> bool:
    """True when a line of the land's output is the repo-lease refusal: another land holds this repository."""
    return any(line.startswith(_REFUSAL_PREFIX) and _BUSY_MARK in line for line in output.splitlines())


def landed(code: int, output: str) -> bool:
    """A land counts only when it exits 0 and the output shows both the merge and the mark_done step."""
    return code == 0 and "merge:" in output and "mark_done:" in output


def land_refusal(action: Action, code: int, output: str) -> Action | None:
    """None on a clean exit; else the needs_chair a land's refusal raises, its cause read from `output` by
    literal substring, else the land fallback.

    "could not apply" is git cherry-pick's own conflict line (its stderr on a conflict, verified against
    `_execute_land_step`'s cherry_pick step); "CONFLICT" never appears there because that step keeps stderr
    over stdout, and cherry-pick writes its "CONFLICT (content): ..." line to stdout, not stderr.
    """
    if code == 0:
        return None
    if "could not apply" in output:
        cause = "conflict"
    elif "failing checks:" in output:
        cause = "checks"
    elif "no branch is exactly one commit ahead of" in output or "no candidate branch found for" in output:
        cause = "missing_branch"
    else:
        cause = "land"
    return {
        "kind": "needs_chair", "initiative": action.get("initiative", ""), "task_id": action.get("task_id", ""),
        "cause": cause, "epoch": action.get("epoch", 0), **_phase_of(action),
    }


def _phase_of(action: Action) -> Action:
    """`{"phase": ...}` for a land_phase, which carries no task_id, so its needs_chair still names what failed; else empty."""
    phase = action.get("phase", "")
    return {"phase": phase} if phase else {}


def _land_subject(action: Action) -> str:
    """The task a land names, else `phase <phase>` for a land_phase; the name a skip or escalation reason quotes."""
    task, phase = action.get("task_id", ""), action.get("phase", "")
    return task if task else (f"phase {phase}" if phase else "")


def branch_pattern(initiative: str) -> str | None:
    """`epic/<initiative>/*`, or None when the initiative is empty or could widen the glob."""
    if not initiative or "/" in initiative or _GLOB_CHARS & set(initiative):
        return None
    return f"epic/{initiative}/*"


def decompose_id(path: str, file_id: str) -> str | Refusal:
    """The initiative id for an intake: its frontmatter id, else the file stem. Never the path; a path-shaped id is refused."""
    chosen = file_id or Path(path).stem
    if not chosen or "/" in chosen or "\\" in chosen or chosen.startswith("."):
        return Refusal(f"intake {path!r} gives initiative id {chosen!r}, which is empty or path-shaped")
    return chosen


def tail(text: str, limit: int) -> str:
    """The last whole lines of `text` that fit in `limit` characters; a single line longer than `limit` keeps its end."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    window = text[-limit:]
    if text[-limit - 1] == "\n":
        return window
    _, newline, rest = window.partition("\n")
    return rest if newline and rest.strip() else window


def argv_for(action: Action, initiative_id: str = "", ids_mode: str = "slug") -> list[str] | None:
    """The cox argv for a land, fetch, launch or pull action; None for any other kind or a missing required field.

    A decompose launch needs `initiative_id`, which `decompose_id` derives from the intake; it is never taken from the path.
    `ids_mode` is the routing profile's `ids:` key: "sequence" appends `--task-ids ordinal` so the graph's own
    tasks come out named `<initiative_id>-t1`, `<initiative_id>-t2`, ...; "slug" (or anything else) appends neither
    flag, today's behaviour.
    """
    kind = action.get("kind")
    # The loop already holds the chair: a command it starts must never claim the loop lease itself (a land that
    # found the record stale took it with an empty host on 2026-09-27 and put the loop in standby).
    task, repo, initiative = action.get("task_id", ""), action.get("repo", ""), action.get("initiative", "")
    run = action.get("run", "")
    phase = action.get("phase", "")
    idea = (action.get("intake_ids") or [""])[0]
    if kind == "land":
        return ["cox", "runs", "land", run, "--task", task, "--repo", repo, "--apply", "--no-claim"] if run and task and repo else None
    if kind == "land_phase":
        return ["cox", "runs", "land", run, "--phase", phase, "--repo", repo, "--apply", "--no-claim"] if run and phase and repo else None
    if kind in ("fetch", "fetch_exit"):
        return ["cox", "runs", "fetch", run] if run else None
    if kind == "launch_decompose":
        task_ids = ["--task-ids", "ordinal"] if ids_mode == "sequence" else []
        decompose_host = action.get("host", "")
        return [
            "cox", "route", "launch", "decompose", "--idea", idea, "--initiative-id", initiative_id, *task_ids, "--no-claim",
            *(["--on", decompose_host] if decompose_host else []),
        ] if idea and initiative_id else None
    if kind == "rescue":
        return ["cox", "route", "launch", "rescue", "--initiative", f"work/{initiative}", "--task", task, "--no-claim"] if initiative and task else None
    if kind in LAUNCH_KINDS:
        host = action.get("host", "")
        return [
            "cox", "route", "launch", "epic", "--initiative", f"work/{initiative}", "--no-claim",
            *(["--repo", repo] if repo else []), *(["--on", host] if host else []),
        ] if initiative else None
    if kind == "pull":
        return ["cox", "route", "pull"]
    if kind == "steer_clear":  # a deferral is only recorded; no command runs
        return None
    return None


def _result(action: Action, status: Status, reason: str = "") -> Result:
    return {"action": action, "status": status, "reason": reason}


def _recorded(result: Result) -> Action:
    """The action with its outcome attached; the reason keeps its last 600 characters, where a traceback's failing frame is."""
    return {**result["action"], "status": result["status"], "reason": tail(result["reason"], _REASON_CAP)}  # type: ignore[typeddict-item]


def _land_result(action: Action, repo: str, code: int, output: str) -> Result:
    """Same as `_result(action, "landed"/"not_landed", output)`, but a true `landed()` also carries the repo's
    new origin/main commit from `land_commit`, run right after so nothing else has moved origin/main yet."""
    if not landed(code, output):
        return _result(action, "not_landed", output)
    return {**_result(action, "landed", output), "commit": land_commit(repo)}


def _land(action: Action, deps: Deps, blocked: dict[str, str]) -> Result:
    repo = action.get("repo", "")
    argv = argv_for(action)
    if argv is None:
        return _result(action, "refused", "land needs a run id, a task_id and a repo")
    if repo in blocked:
        return _result(action, "skipped", f"an earlier land in {repo} ({blocked[repo]}) was not counted")
    code, output = deps.run(argv)
    # Busy first: a repo-lease refusal exits non-zero like every other refusal, so `land_refusal` would escalate it.
    if _repo_busy(output):
        return _result(action, "busy", output)
    refusal = land_refusal(action, code, output)
    if refusal is not None:
        return {"action": action, "status": "refused", "reason": output, "needs_chair": refusal}
    return _land_result(action, repo, code, output)


def _land_async(action: Action, deps: Deps, sink: LandSink) -> Result:
    """Hand the land to the sink and return at once; a repo whose land is still out is left alone."""
    repo = action.get("repo", "")
    if sink.pending(repo):
        return _result(action, "in_progress", f"a land in {repo} is still running")
    land = _land if action.get("kind") == "land" else _land_phase
    return sink.submit(action, lambda: land(action, deps, {}))


def _swept_branches(deps: Deps, repo: str, pattern: str, carry: set[str]) -> tuple[list[str], str]:
    """Like `deps.delete_branches`, but a branch on a carried phase is named and kept, not deleted.

    `deps.delete_branches` force-deletes every branch under `pattern` with no way to exempt one, so a carried
    sweep lists the matches itself and deletes only those not on a carried phase.
    """
    if not carry:
        return deps.delete_branches(repo, pattern)
    code, listing = deps.run(["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}"])
    if code != 0:
        return [], listing or f"git for-each-ref exited {code}"
    prefix = pattern.removesuffix("*")
    names = [n for n in listing.split() if n.startswith(prefix) and n.rsplit("/", 1)[-1] not in carry]
    outcomes = [(name, deps.run(["git", "-C", repo, "branch", "-D", name])) for name in names]
    deleted = [name for name, (c, _) in outcomes if c == 0]
    return deleted, "".join(out for _, (c, out) in outcomes if c != 0)


def _over_ssh(deps: Deps, ssh: str) -> Deps:
    """`deps` with its git door, and the sweep built on it, running on the host at `ssh` instead of here."""
    def run(argv: list[str]) -> tuple[int, str]:
        return deps.run(ssh_argv(ssh, argv))

    return replace(deps, run=run, delete_branches=partial(delete_branches_with, run))


def _pruned(deps: Deps, repo: str, initiative: str, pattern: str, carry: set[str]) -> tuple[list[str], str]:
    """The clear's worktree prune, then its branch sweep, through `deps.run`; (deleted branches, failure line or "")."""
    code, listing = deps.run(["git", "-C", repo, "worktree", "list", "--porcelain"])
    if code != 0:
        return [], f"git worktree list in {repo}: {listing.strip()}"
    entries = chair_plan_prune.worktrees_to_prune(listing, initiative)
    for argv in chair_plan_prune.prune_argv(entries, carry):
        code, output = deps.run(["git", "-C", repo, *argv[1:]])
        if code != 0:
            return [], f"{' '.join(argv)} in {repo}: {output.strip()}"
    # The prune already deleted its entries' branches, so the sweep finding nothing after a prune is success, not a miss.
    swept, error = _swept_branches(deps, repo, pattern, carry)
    deleted = [e["branch"] for e in entries if e["branch"].rsplit("/", 1)[-1] not in carry] + swept
    return deleted, f"deleted {deleted} in {repo}; git: {error.strip()}" if error else ""


def _branch_on_host(deps: Deps, at_home: Run, ssh: str, repo: str, branch: str) -> tuple[bool, str]:
    """(present on the host, failure line or ""). A branch only the chair holds is pushed to the host first, so
    the relaunch there builds on it; a branch on neither machine is nothing to carry."""
    verify = ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]
    on_host, host_out = at_home(verify)
    if on_host == 124:
        return False, f"git rev-parse of {branch} on {ssh}: {host_out.strip()}"
    if on_host == 0:
        return True, ""
    if deps.run(verify)[0] != 0:
        return False, ""
    code, out = deps.run(["git", "-C", repo, "push", f"{ssh}:{repo}", f"refs/heads/{branch}:refs/heads/{branch}"])
    return (True, "") if code == 0 else (False, f"git push of {branch} to {ssh}:{repo}: {out.strip()}")


def _carry_forward(
    action: Action, deps: Deps, repo: str, initiative: str, phases: list[str], ssh: str = ""
) -> tuple[list[str], Action | None, Result | None]:
    """Merge main into each carried phase's branch, one throwaway worktree at a time; the repo the running loop
    imports from is never checked out or switched.

    A non-empty `ssh` carries on the action's `host` instead, after syncing that host's main, because the
    relaunch runs there and reads that host's copy of the branch. A branch on neither machine is skipped.

    Returns the phases actually merged, an extra needs_chair action for the first conflict (or None), and a
    failed Result that stops the clear immediately (or None). At most one of the last two is ever set: a
    conflict stops the loop without failing the clear, any other failed git step fails it.
    """
    host = action.get("host", "")
    at_home = _over_ssh(deps, ssh).run if ssh else deps.run
    if ssh and phases:
        # The relaunch's own launch_on_host runs this same sync before starting the lane, so a host checkout that
        # is dirty or off main would fail that relaunch anyway; failing here says why one step earlier.
        code, out = at_home(sync_argv(repo))
        if code != 0:
            return [], None, _result(action, "failed", f"updating {repo} on {host} before the carry: {out.strip()}")
    merged: list[str] = []
    for phase in phases:
        branch = f"epic/{initiative}/{phase}"
        present, error = _branch_on_host(deps, at_home, ssh, repo, branch) if ssh else (True, "")
        if error:
            return merged, None, _result(action, "failed", error)
        if not present:
            continue
        if ssh:
            code, out = at_home(["mktemp", "-d", "/tmp/cox-carry-XXXXXX"])
            tmp = out.strip().splitlines()[-1] if code == 0 and out.strip() else ""
            if not tmp:
                return merged, None, _result(action, "failed", f"mktemp -d on {host}: {out.strip()}")
        else:
            tmp = tempfile.mkdtemp(prefix="cox-carry-")
        code, out = at_home(["git", "-C", repo, "worktree", "add", tmp, branch])
        if code != 0:
            return merged, None, _result(action, "failed", f"git worktree add {tmp} {branch} in {repo}: {out.strip()}")
        code, out = at_home(["git", "-C", tmp, "merge", "--no-edit", "main"])
        if code != 0:
            _, diff_out = at_home(["git", "-C", tmp, "diff", "--name-only", "--diff-filter=U"])
            conflicted = [line for line in diff_out.splitlines() if line.strip()]
            if not conflicted:
                at_home(["git", "-C", repo, "worktree", "remove", "--force", tmp])
                return merged, None, _result(action, "failed", f"git merge --no-edit main in {tmp} ({branch}): {out.strip()}")
            at_home(["git", "-C", tmp, "merge", "--abort"])
            rcode, rout = at_home(["git", "-C", repo, "worktree", "remove", "--force", tmp])
            if rcode != 0:
                return merged, None, _result(action, "failed", f"git worktree remove --force {tmp} in {repo}: {rout.strip()}")
            needs_chair: Action = {
                "kind": "needs_chair", "initiative": initiative, "cause": "carry_conflict", "epoch": action.get("epoch", 0),
                "reason": f"phase {phase} conflicts with main: {conflicted}",
            }
            return merged, needs_chair, None
        rcode, rout = at_home(["git", "-C", repo, "worktree", "remove", "--force", tmp])
        if rcode != 0:
            return merged, None, _result(action, "failed", f"git worktree remove --force {tmp} in {repo}: {rout.strip()}")
        merged.append(phase)
    return merged, None, None


def _land_phase(action: Action, deps: Deps, blocked: dict[str, str]) -> Result:
    """Same shape as `_land`, one command for a whole phase: its own `mark_done:` lines mark every task in the
    phase done, so this only runs the command and reads its output."""
    repo = action.get("repo", "")
    argv = argv_for(action)
    if argv is None:
        return _result(action, "refused", "land_phase needs a run id, a phase and a repo")
    if repo in blocked:
        return _result(action, "skipped", f"an earlier land in {repo} ({blocked[repo]}) was not counted")
    code, output = deps.run(argv)
    # Busy first: a repo-lease refusal exits non-zero like every other refusal, so `land_refusal` would escalate it.
    if _repo_busy(output):
        return _result(action, "busy", output)
    refusal = land_refusal(action, code, output)
    if refusal is not None:
        return {"action": action, "status": "refused", "reason": output, "needs_chair": refusal}
    return _land_result(action, repo, code, output)


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
    if deps.lands is not None and deps.lands.pending(repo):
        return _result(action, "skipped", f"a land in {repo} is still running")
    host = action.get("host", "")
    ssh = deps.ssh_for(host) if host else ""
    if host and not ssh:
        return _result(action, "failed", f"no ssh destination for host {host}: its clear cannot run there")
    carry = set(action.get("carry", []))
    deleted, error = _pruned(deps, repo, initiative, pattern, carry)
    if error:
        return _result(action, "failed", error)
    # A host-homed initiative's earlier runs left their worktrees and branches in the host's checkout, and git
    # refuses to add a worktree on a branch another worktree holds, so the host gets the same prune first.
    host_deleted, error = _pruned(_over_ssh(deps, ssh), repo, initiative, pattern, carry) if ssh else ([], "")
    if error:
        return _result(action, "failed", f"on {host}: {error}")
    merged, needs_chair, failure = _carry_forward(action, deps, repo, initiative, sorted(carry), ssh)
    if failure is not None:
        return failure
    where = f" on {host}" if host else ""
    parts = [
        p
        for p in (
            f"deleted {deleted} in {repo}" if deleted else "",
            f"deleted {host_deleted} in {repo}{where}" if host_deleted else "",
            f"carried {merged} past main{where}" if merged else "",
        )
        if p
    ]
    # Nothing stale or partial is the state a clear exists to reach: a relaunch that failed after an earlier clear
    # must not be skipped forever because that clear already deleted the branches and carried the phases forward.
    result = _result(action, "done", "; ".join(parts) if parts else f"nothing to clear: no branch matched {pattern} in {repo}")
    return {**result, "needs_chair": needs_chair} if needs_chair is not None else result


def _lease(action: Action, deps: Deps) -> Result:
    holder, host = action.get("holder", ""), action.get("host", "")
    over_expired = action.get("reason", "").startswith("takeover expired")
    line = deps.acquire_lease(holder, host, steal=True) if over_expired else deps.acquire_lease(holder, host)
    return _result(action, "refused", line) if line else _result(action, "done")


def _rendered_value(value: object) -> object:
    """A list renders as the flat-list literal `_frontmatter` writes and `parse_frontmatter` reads back; anything else is unchanged."""
    if isinstance(value, list):
        return route._Raw("[" + ", ".join(route._yaml_scalar(v) for v in value) + "]")
    return value


def _initiative_tickets(work_dir: Path, initiative: str) -> list[dict]:
    """Edge. Every ticket under `work/<initiative>/*/*.md`, `work_item`'s fields plus `body` for `merge_same_phase`.

    A missing initiative directory reads as no tickets, not an error.
    """
    tickets = []
    for task_path in sorted((work_dir / "work" / initiative).glob("*/*.md")):
        if task_path.name == "initiative.md":
            continue
        try:
            text = task_path.read_text(encoding="utf-8")
        except OSError:
            continue
        fields, body = route.parse_frontmatter(text)
        item = route.work_item(fields, initiative=initiative, phase_dir=task_path.parent.name, stem=task_path.stem)
        tickets.append({**item, "body": body})
    return tickets


def _ticket_paths(work_dir: Path, initiative: str) -> dict[str, Path]:
    """Edge. Every ticket path under `work/<initiative>/*/*.md`, keyed by its task id (file stem); `initiative.md` excluded."""
    return {
        path.stem: path
        for path in sorted((work_dir / "work" / initiative).glob("*/*.md"))
        if path.name != "initiative.md"
    }


def _write_ticket(work_dir: Path, item: dict) -> None:
    """Edge. `item` re-rendered to frontmatter and written back to `work_dir/work/<initiative>/<file>`, the shape `_initiative_tickets` read it from."""
    fields = [(key, _rendered_value(value)) for key, value in item.items() if key not in ("initiative", "file", "body")]
    text = route._frontmatter(fields, item.get("body", ""))
    (work_dir / "work" / item["initiative"] / item["file"]).write_text(text, encoding="utf-8")


def _merge_and_write(work_dir: Path, initiative: str) -> None:
    """Same-phase ready/todo tickets of `initiative` that share a surface collapse into one; the merged ticket and
    every dropped member are written back to disk before the next build starts, so it sees one ticket where two
    used to collide."""
    tickets = _initiative_tickets(work_dir, initiative)
    if not tickets:
        return
    merged_items, merges = route.merge_same_phase(tickets)
    by_id = {item["id"]: item for item in merged_items}
    changed_ids = {member_id for merge in merges for member_id in merge["members"]}
    for ticket_id in changed_ids:
        item = by_id.get(ticket_id)
        if item is not None:
            _write_ticket(work_dir, item)


def _launch(action: Action, deps: Deps) -> Result:
    initiative_id: str | Refusal = ""
    if action.get("kind") == "launch_decompose" and action.get("intake_ids"):
        idea = action["intake_ids"][0]
        initiative_id = decompose_id(idea, deps.intake_id(idea))
    if isinstance(initiative_id, Refusal):
        return _result(action, "refused", initiative_id.reason)
    if action.get("kind") in ("launch_epic", "relaunch") and action.get("initiative"):
        _merge_and_write(deps.work_dir, action["initiative"])
    argv = argv_for(action, initiative_id, deps.ids_mode)
    if argv is None:
        return _result(action, "refused", f"{action.get('kind')} names no initiative, task or intake id")
    code, output = deps.run(argv)
    return _result(action, "done" if code == 0 else "failed", output)


def _fetched_host(runs_dir: Path, run: str) -> str:
    """Edge. The host recorded in `<run>.remote.json`; "" when the file is missing or unparseable."""
    try:
        text = remote_lane.remote_record_path(runs_dir, run).read_text(encoding="utf-8")
    except OSError:
        return ""
    record = remote_lane.parse_remote_record(text)
    return record["host"] if record else ""


def _fetch_exit(action: Action, deps: Deps) -> Result:
    """`cox runs fetch` for a remote run, then fold its approvals into local ticket state on success.

    A nonzero exit applies nothing and reports failed, exactly as `fetch` does; the fetch_exit stays
    unresolved for the next tick to retry, since it never landed.
    """
    run, initiative = action.get("run", ""), action.get("initiative", "")
    argv = argv_for(action)
    if argv is None:
        return _result(action, "refused", "fetch_exit needs a run id")
    code, output = deps.run(argv)
    if code != 0:
        return _result(action, "failed", output)
    approved = chair_apply_fetch.apply_fetched_approvals(deps.runs_dir, deps.work_dir, run, initiative)
    reason = f"fetched {run}; approved: {', '.join(approved)}"
    return {"action": action, "status": "done", "reason": reason, "run": run, "host": _fetched_host(deps.runs_dir, run)}


def _widen_ticket(action: Action, deps: Deps) -> Result:
    """In-process, with no argv; a widened ticket's `ready` is mirrored to the store so file and store stay one record."""
    ok, reason = chair_apply_widen.apply_widen(deps.work_dir, action, deps.now()[:10])
    if not ok:
        return _result(action, "failed", reason)
    warning = store_cli.mirror_state(deps.runs_dir, action.get("initiative", ""), action.get("task_id", ""), "ready", "chair")
    return _result(action, "done", reason if warning is None else f"{reason}; {warning}")


def _prune_available(run: Run, harness_python: str) -> bool:
    """Edge. True only when `<harness_python> -m harness.store_backfill_traces prune --help` exits 0."""
    code, _ = run([harness_python, "-m", "harness.store_backfill_traces", "prune", "--help"])
    return code == 0


def _housekeeping(action: Action, deps: Deps) -> Result:
    """Lake sync, trace prune, runs clean, in order; the reason names all three, none stopping the others."""
    traces_root = run_store._traces_root(deps.runs_dir).url
    status, reason = chair_housekeeping.run_housekeeping(
        deps.run, traces_root, partial(_prune_available, deps.run, deps.harness_python),
        deps.harness_python, deps.log_retention_days,
    )
    return _result(action, status, reason)


def _send_note(deps: Deps, ref: courier.Reference, text: str) -> None:
    """Edge. One courier line for `ref` appended to `work_dir/courier.jsonl`, addressed to `deps.note_to`."""
    sender = (chair.read(deps.runs_dir) or {}).get("session") or "chair"
    entry = courier.send(ref, sender, deps.note_to, text, uuid.uuid4().hex)
    path = deps.work_dir / "courier.jsonl"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(courier.append_line(existing, entry), encoding="utf-8")


def _send_stale_draft_note(deps: Deps, initiative: str, reason: str) -> None:
    """Edge. One `coxswain://initiative/<id>` courier line to `deps.note_to`, quoting `reason` verbatim."""
    _send_note(deps, courier.Reference("initiative", initiative), reason)


def lane_writer(profile_path: Path) -> Callable[[str, int], int]:
    """The `Deps.set_lanes` that writes through `cox settings set host <host>.capacity <n>`, the one lane write path."""
    return lambda host, n: cox_settings.run_set(profile_path, "host", f"{host}.capacity", str(n), False)


def _tune_lanes(action: Action, deps: Deps) -> Result:
    """Edge. Sets the host's lane count through `deps.set_lanes`. The bounds are `min_lanes` and `max_lanes` in
    the action's evidence; a missing bound or a count outside it is refused with nothing written."""
    host = action.get("host", "")
    to_lanes = action.get("to_lanes")
    evidence = action.get("evidence", {})
    low, high = evidence.get("min_lanes"), evidence.get("max_lanes")
    if not host or not isinstance(to_lanes, int) or isinstance(to_lanes, bool) or deps.set_lanes is None:
        return _result(action, "refused", "tune_lanes needs a host, an integer to_lanes and a wired lane writer")
    if not isinstance(low, int) or not isinstance(high, int):
        return _result(action, "refused", "tune_lanes needs min_lanes and max_lanes in its evidence")
    if not low <= to_lanes <= high:
        return _result(action, "refused", f"{host}: {to_lanes} lanes is outside {low} to {high}")
    if deps.set_lanes(host, to_lanes) != 0:
        return _result(action, "failed", f"{host}: setting {to_lanes} lanes failed")
    return _result(action, "done", f"{host}: lanes {action.get('from_lanes', '?')} to {to_lanes}: {action.get('reason', '')}")


def _propose_tiers(action: Action, deps: Deps) -> Result:
    """Edge. One courier line carrying the action's rendered body. Propose-only: no settings writer is called."""
    body = action.get("body", "")
    if not body:
        return _result(action, "refused", "propose_tiers needs a body")
    _send_note(deps, courier.Reference("proposal", "tiers"), body)
    return _result(action, "done", "proposed tiers to the inbox")


def _stale_to_draft(action: Action, deps: Deps) -> Result:
    """Edge. Reads the initiative and its tickets, asks `stale_draft.plan_stale_draft` to turn the stale ones into
    a draft, and on success writes the rewritten files and tells `deps.note_to` by courier.

    A Refusal (an initiative already a draft, no stale task, an unknown ticket) writes nothing and sends no
    note; it is reported as an ordinary refusal, the same as any other action's precondition failure.
    """
    initiative = action.get("initiative", "")
    initiative_path = deps.work_dir / "work" / initiative / "initiative.md"
    try:
        initiative_text = initiative_path.read_text(encoding="utf-8")
    except OSError:
        return _result(action, "refused", f"no initiative.md at {initiative_path}")
    ticket_paths = _ticket_paths(deps.work_dir, initiative)
    ticket_texts = {}
    for task_id, path in ticket_paths.items():
        try:
            ticket_texts[task_id] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    stale_tasks = action.get("stale_tasks", [])
    reason = action.get("reason", "")
    plan = stale_draft.plan_stale_draft(initiative_text, ticket_texts, stale_tasks, reason, action.get("since", ""), deps.now())
    if isinstance(plan, stale_draft.Refusal):
        return _result(action, "refused", plan.reason)
    initiative_path.write_text(plan.initiative_text, encoding="utf-8")
    for task_id in stale_tasks:
        ticket_paths[task_id].write_text(plan.tickets[task_id], encoding="utf-8")
    _send_stale_draft_note(deps, initiative, reason)
    return _result(action, "done", f"drafted {initiative}: stale tasks {stale_tasks}")


def _check_login(action: Action, deps: Deps) -> Result:
    """Runs the injected check_login edge against the action's host; the row it prints becomes the reason.
    Never touches the attempts table, so its outcome is always "recorded", the same status standby and
    mark_lost carry."""
    host = action.get("host", "")
    if not host or deps.check_login is None:
        return _result(action, "refused", "check_login needs a host and a wired check_login edge")
    row = deps.check_login(host)
    return _result(action, "recorded", json.dumps(row, sort_keys=True))


def _stalled(action: Action, deps: Deps, sig: int) -> Result:
    """Edge. One `sig` to the pid in `<run>.pid`; a missing pidfile or a failed send is a failed result, never a raise."""
    pidfile = Path(deps.runs_dir) / f'{action.get("run", "")}.pid'
    try:
        text = pidfile.read_text(encoding="utf-8")
    except OSError:
        return _result(action, "failed", "no pidfile")
    pid = route.parse_pid(text)
    if pid is None:
        return _result(action, "failed", "no pidfile")
    try:
        deps.send_signal(pid, sig)
    except OSError as exc:
        return _result(action, "failed", f"signal {pid}: {exc}")
    return _result(action, "done")


def _review_landed(action: Action, deps: Deps) -> Result:
    """Edge. `chair_apply_review.apply_review` over this tick's store, git and ssh doors."""

    def delete_branches(ssh: str, repo: str, pattern: str) -> tuple[list[str], str]:
        return (_over_ssh(deps, ssh) if ssh else deps).delete_branches(repo, pattern)

    outcome = chair_apply_review.apply_review(
        action,
        set_state=partial(store_cli.set_state, deps.runs_dir),
        mark_landed=partial(store_cli.mark_landed, deps.runs_dir),
        delete_branches=delete_branches,
        ssh_for=deps.ssh_for,
        now=deps.now,
    )
    if outcome.status != "needs_chair":
        return _result(action, outcome.status, outcome.reason)
    raised: Action = {
        "kind": "needs_chair", "initiative": action.get("initiative", ""), "phase": action.get("phase", ""),
        "task_id": action.get("task_id", ""), "url": action.get("url", ""), "cause": outcome.cause,
        "epoch": action.get("epoch", 0), "reason": outcome.reason,
    }
    return {**_result(action, "refused", outcome.reason), "needs_chair": raised}


def _carry_phase(action: Action, deps: Deps) -> Result:
    """Edge. `perform_carry` over the wired ports. A stop comes back refused and carrying its needs_chair, the shape
    `_review_landed` returns; a port failure is a failed result, never a raise out of perform."""
    if deps.carry_ports is None:
        return _result(action, "refused", "carry_phase needs wired carry ports")
    git, forge, store = deps.carry_ports(action)
    try:
        result = perform_carry(action, git, forge, store)
    except Exception as exc:  # the edge: a port failure becomes a failed result
        return _result(action, "failed", f"carry of {action.get('phase', '')} failed: {exc}")
    finally:
        close = getattr(git, "close", None)  # the real git edge's worktree; a fake has none
        if close is not None:
            close()
    stop = result["action"]
    if result["status"] == "escalated" and stop.get("kind") == "needs_chair":
        return {**_result(action, "refused", result["reason"]), "needs_chair": stop}
    return {**result, "action": action}


def _rebase_phase(action: Action, deps: Deps) -> Result:
    """Edge. `perform_rebase` over the wired port; its own refusal already carries the needs_chair."""
    if deps.rebase_port is None:
        return _result(action, "refused", "rebase_phase needs a wired rebase port")
    return perform_rebase(action, deps.rebase_port(action))


def revert_outcome(action: Action, reverted: RevertResult) -> tuple[str, str]:
    """The needs_chair (cause, reason) for a revert: main_red once merged, else revert_failed, which says main is still red."""
    if reverted.status == MERGED:
        return "main_red", f"main went red after PR #{action.get('pr', 0)}; reverted {action.get('commit', '')} in {reverted.pr}"
    return "revert_failed", f"{reverted.detail}; main is still red after the revert of {action.get('commit', '')}"


def _revert_land(action: Action, deps: Deps) -> Result:
    """Edge. `perform_revert` over the wired ports, then quarantine the phase whatever the status, so no relaunch stacks onto
    a red main. The land is resolved as reverted only when the revert merged; a failed one is planned again next tick."""
    if deps.revert_ports is None or deps.quarantine_phase is None or deps.resolve_land is None:
        return _result(action, "refused", "revert_land needs wired revert ports, quarantine and resolve writers")
    initiative, phase = action.get("initiative", ""), action.get("phase", "")
    git, forge = deps.revert_ports(action)
    try:
        reverted = perform_revert(action.get("repo", ""), action.get("pr", 0), action.get("commit", ""), action.get("reason", ""), git, forge)
    except Exception as exc:  # the edge: a port failure is a failed revert, and main is still red
        reverted = RevertResult("failed", "", f"revert raised {type(exc).__name__}: {exc}")
    deps.quarantine_phase(initiative, phase, "main_red", action.get("reason", ""))
    if reverted.status == MERGED:
        deps.resolve_land(initiative, phase, "reverted")
    cause, reason = revert_outcome(action, reverted)
    raised: Action = {
        "kind": "needs_chair", "initiative": initiative, "phase": phase, "cause": cause,
        "epoch": action.get("epoch", 0), "reason": reason,
    }
    return {**_result(action, "done" if reverted.status == MERGED else "failed", reason), "needs_chair": raised}


def _steer_reason(action: Action) -> str:
    """`steer clear of <other>: <paths joined by comma>`, cut to 200 characters."""
    return f"steer clear of {action.get('other', '')}: {','.join(action.get('paths', []))}"[:200]


def _execute(action: Action, deps: Deps, blocked: dict[str, str]) -> Result:
    kind = action.get("kind")
    if (
        kind in ("land", "land_phase") and deps.lands is not None
        and argv_for(action) is not None and action.get("repo", "") not in blocked
    ):
        return _land_async(action, deps, deps.lands)
    if kind == "land":
        return _land(action, deps, blocked)
    if kind == "land_phase":
        return _land_phase(action, deps, blocked)
    if kind == "clear_branches":
        return _clear(action, deps, blocked)
    if kind == "review_landed":
        return _review_landed(action, deps)
    if kind == "carry_phase":
        return _carry_phase(action, deps)
    if kind == "rebase_phase":
        return _rebase_phase(action, deps)
    if kind == "revert_land":
        return _revert_land(action, deps)
    if kind == "take_lease":
        return _lease(action, deps)
    if kind == "check_login":
        return _check_login(action, deps)
    if kind in ("standby", "mark_lost"):
        return _result(action, "recorded")
    if kind == "needs_chair":
        return _result(action, "recorded", action.get("reason", ""))  # `_recorded` overwrites reason, so carry it in the result
    if kind == "steer_clear":
        return _result(action, "recorded", _steer_reason(action))
    if kind == "fetch_exit":
        return _fetch_exit(action, deps)
    if kind == "widen_ticket":
        return _widen_ticket(action, deps)
    if kind in LAUNCH_KINDS or kind in ("pull", "fetch"):
        return _launch(action, deps)
    if kind == "housekeeping":
        return _housekeeping(action, deps)
    if kind == "stale_to_draft":
        return _stale_to_draft(action, deps)
    if kind == "tune_lanes":
        return _tune_lanes(action, deps)
    if kind == "propose_tiers":
        return _propose_tiers(action, deps)
    if kind == "stalled_usr1":
        return _stalled(action, deps, signal.SIGUSR1)
    if kind == "stalled_stop":
        return _stalled(action, deps, signal.SIGTERM)
    return _result(action, "refused", f"unsupported action kind {kind!r}")


def notices_to_ack(tasks: list[dict], notices: list[dict]) -> list[str]:
    """Ids of the open land notices whose task is done, in first-seen order.

    A land notice is an unacked `coxswain://task/<id>` entry whose note mentions landing. Acked entries and
    unknown tasks yield nothing, so acking twice or acking an absent notice is a no-op.
    """
    done = {t["id"] for t in tasks if t.get("state") == "done"}
    prefix = "coxswain://task/"
    return [
        n["id"]
        for n in notices
        if not n.get("ack") and str(n.get("ref", "")).startswith(prefix)
        and str(n["ref"])[len(prefix):] in done and "land" in str(n.get("note", "")).lower()
    ]


def _ack_done_land_notices(deps: Deps) -> list[str]:
    """Edge. Acks, through `courier.ack_file`, every open land notice in `work_dir/courier.jsonl` whose task is
    done under `work_dir/work/*/*/*.md`; returns the ids acked. A missing or unreadable file acks nothing."""
    path = deps.work_dir / "courier.jsonl"
    try:
        notices = courier.entries(path.read_text(encoding="utf-8"))
    except OSError:
        return []
    if not any(not n.get("ack") for n in notices):
        return []
    tasks = []
    for task_path in sorted((deps.work_dir / "work").glob("*/*/*.md")):
        try:
            fields, _ = route.parse_frontmatter(task_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        tasks.append({"id": fields.get("id", task_path.stem), "state": fields.get("state", "todo")})
    acked = []
    for message_id in notices_to_ack(tasks, notices):
        try:
            if courier.ack_file(path, message_id):
                acked.append(message_id)
        except OSError:
            continue
    return acked


def _note_uncounted(action: Action, blocked: dict[str, str], blocked_initiatives: dict[str, str]) -> None:
    """A land_phase that names an initiative blocks only that initiative; any other land blocks its whole repo."""
    initiative = action.get("initiative", "")
    if action.get("kind") == "land_phase" and initiative:
        blocked_initiatives[initiative] = _land_subject(action)
    else:
        blocked[action.get("repo", "")] = _land_subject(action)


def perform(actions: list[Action], deps: Deps, current_epoch: Callable[[], int], dry_run: bool) -> list[Result]:
    """Edge. One result per action, in order; each is recorded after it runs.

    The epoch is re-read per action. standby and take_lease are never fenced. A dry run touches and records nothing.
    A relaunch whose initiative had a clear_branches end other than done earlier this tick is skipped.
    A land or land_phase whose command exits nonzero is refused, escalated with the cause `land_refusal` read
    from its output, and blocks its repo's later lands and land_phases this tick, except that a land_phase naming
    an initiative blocks only that initiative's later lands, clear_branches and relaunches. A land refused before
    its command runs blocks nothing.
    With a land sink, a land returns `in_progress` at once and the actions after it run in the same tick. The lands
    that finished since the last tick are collected first, so their results lead this tick's.
    """
    results: list[Result] = []
    blocked: dict[str, str] = {}  # repo -> task or phase of the uncounted land or land_phase that blocks its later lands and deletes
    blocked_initiatives: dict[str, str] = {}  # initiative -> phase of its uncounted land_phase that skips its later lands
    uncleared: dict[str, str] = {}  # initiative -> status of its clear_branches that did not finish done, this tick
    if not dry_run:
        _ack_done_land_notices(deps)
    for finished in deps.lands.collect() if deps.lands is not None and not dry_run else []:
        land = finished["action"]
        if finished["status"] == "not_landed" or "needs_chair" in finished:
            _note_uncounted(land, blocked, blocked_initiatives)
        deps.record(_recorded(finished))
        results.append(finished)
        if finished["status"] not in ("landed", "fenced", "busy"):
            escalated = _escalate(land, finished)
            deps.record(_recorded(escalated))
            results.append(escalated)
    for action in actions:
        if dry_run:
            results.append(_result(action, "dry_run"))
            continue
        initiative = action.get("initiative", "")
        if action.get("kind") not in _UNFENCED and is_fenced(action, current_epoch()):
            result = _result(action, "fenced", "planned under another lease epoch")
        elif action.get("kind") == "relaunch" and initiative in uncleared:
            result = _result(action, "skipped", f"clear_branches for {initiative} was {uncleared[initiative]} this tick")
        elif initiative in blocked_initiatives and action.get("kind") in ("land", "land_phase", "clear_branches", "relaunch"):
            result = _result(action, "skipped", f"an earlier land in {action.get('repo', '')} ({blocked_initiatives[initiative]}) was not counted")
        else:
            result = _execute(action, deps, blocked)
        if action.get("kind") in ("land", "land_phase") and (result["status"] == "not_landed" or "needs_chair" in result):
            _note_uncounted(action, blocked, blocked_initiatives)
        if action.get("kind") == "clear_branches" and result["status"] != "done":
            uncleared[initiative] = result["status"]
        if result["status"] != "in_progress":  # the land's own result is recorded when it is collected
            deps.record(_recorded(result))
        results.append(result)
        if action.get("kind") in ("land", "land_phase") and result["status"] not in ("landed", "fenced", "busy", "in_progress"):
            escalated = _escalate(action, result)
            deps.record(_recorded(escalated))
            results.append(escalated)
        if action.get("kind") in ("clear_branches", "review_landed", "carry_phase", "rebase_phase", "revert_land") and "needs_chair" in result:
            carried = _result(result["needs_chair"], "recorded", result["needs_chair"].get("reason", ""))
            deps.record(_recorded(carried))
            results.append(carried)
    return results


def escalation(land: Action) -> Action:
    """The needs_chair a land that did not land raises when its command gave no refusal to classify: the facts
    dropped its stranded row, so this reports it instead."""
    return {
        "kind": "needs_chair", "initiative": land.get("initiative", ""), "task_id": land.get("task_id", ""),
        "cause": STRANDED_CAUSE, "epoch": land.get("epoch", 0), **_phase_of(land),
    }


def _escalate(land: Action, result: Result) -> Result:
    raised = result.get("needs_chair") or escalation(land)
    return _result(raised, "escalated", f"land {_land_subject(land)} {result['status']}")


_SMOKE_REPOS = frozenset({"coxswain-tools", "coxswain-graphs"})
_PR_URL_RE = re.compile(r"^pr_create: \S*/pull/(\d+)\s*$", re.MULTILINE)  # only the step line, never a stray /pull/ elsewhere


def _pr_from_reason(reason: str) -> int:
    """The PR number a land's captured output names: its `pr_create: <url>` line (`cli.py`'s `_land_walk`)
    always runs before `merge:`/`mark_done:` on a true `landed()` result, so it is already in `reason`; 0
    when no such line is there."""
    match = _PR_URL_RE.search(reason)
    return int(match[1]) if match else 0


def smoke_targets(results: list[Result]) -> list[LandTrigger]:
    """A `LandTrigger` for every landed result in coxswain-tools or coxswain-graphs, for the tick wiring's
    post-land smoke; any other repo, including an umbrella/meta one, is skipped. An action's repo is a checkout
    path, not a bare name, so the match is on its last component and the trigger keeps the full path for `git -C`."""
    return [
        {"repo": r["action"].get("repo", ""), "pr": _pr_from_reason(r["reason"]), "commit": r.get("commit", "")}
        for r in results
        if r["status"] == "landed" and Path(r["action"].get("repo", "")).name in _SMOKE_REPOS
    ]


def _text(captured: str | bytes | None) -> str:
    """`TimeoutExpired` hands back bytes or str, or None when nothing was printed."""
    return captured.decode(errors="replace") if isinstance(captured, bytes) else captured or ""


def run_argv(argv: list[str], cwd: Path | None = None, timeout: float | None = None) -> tuple[int, str]:
    """Edge. A missing binary is exit 127 and a timeout exit 124, each with its message, never an exception out of perform.

    A timeout keeps what the child printed before it was killed, after the message, so a later task can read it."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd, timeout=timeout)
    except OSError as error:
        return 127, f"{argv[0] if argv else '<empty argv>'}: {error}"
    except subprocess.TimeoutExpired as error:
        message = f"{argv[0] if argv else '<empty argv>'}: timed out after {timeout}s\n"
        return 124, message + _text(error.stdout) + _text(error.stderr)
    return done.returncode, done.stdout + done.stderr


def run_lane_host(argv: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Edge. One ssh against a lane host, bounded: a hung host comes back as exit 124 carrying its partial output."""
    return run_argv(argv, cwd, timeout=LANE_HOST_TIMEOUT_S)


def reaches_lane_host(argv: list[str]) -> bool:
    """An `ssh` argv, or a `git push`, which the chair only sends to a lane host's `<ssh>:<repo>`."""
    return argv[:1] == ["ssh"] or (argv[:1] == ["git"] and "push" in argv)


def _bounded_for_ssh(cwd: Path) -> Run:
    """Edge. A door that bounds an argv reaching a lane host by `LANE_HOST_TIMEOUT_S`; local argvs stay unbounded."""
    def run(argv: list[str]) -> tuple[int, str]:
        return run_lane_host(argv, cwd) if reaches_lane_host(argv) else run_argv(argv, cwd)

    return run


def _local_only(cwd: Path) -> Run:
    """Edge. A dry run's door: a local argv runs as usual, one that reaches a lane host is refused unrun with exit 1."""
    def run(argv: list[str]) -> tuple[int, str]:
        return (1, NOT_PROBED) if reaches_lane_host(argv) else run_argv(argv, cwd)

    return run


def _not_probed(argv: list[str]) -> None:
    return None


def fenced_fetch(fetch: Fetch, dry_run: bool) -> Fetch:
    """The door a remote-lane probe reaches a host by: `fetch` itself live, and one that never dials in a dry run."""
    return _not_probed if dry_run else fetch


def land_commit(repo_dir: str) -> str:
    """Edge. `origin/main`'s commit hash in `repo_dir`, read right after a true `landed()` result so it reflects
    the land that just happened before anything else can move origin/main; "" on any git failure."""
    code, output = run_argv(["git", "-C", repo_dir, "log", "-1", "--format=%H", "origin/main"])
    return output.strip() if code == 0 else ""


def delete_branches_with(run: Run, repo: str, pattern: str) -> tuple[list[str], str]:
    """Force-delete local branches under `pattern` in `repo`; (deleted names, error text)."""
    code, listing = run(["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}"])
    if code != 0:
        return [], listing or f"git for-each-ref exited {code}"
    prefix = pattern.removesuffix("*")
    outcomes = [(name, run(["git", "-C", repo, "branch", "-D", name])) for name in listing.split() if name.startswith(prefix)]
    deleted = [name for name, (c, _) in outcomes if c == 0]
    return deleted, "".join(out for _, (c, out) in outcomes if c != 0)


def read_intake_id(workspace: Path, path: str) -> str:
    """Edge. The `id` field of the intake file at `workspace/path`; "" when unreadable or absent."""
    try:
        fields, _ = route.parse_frontmatter((workspace / path).read_text(encoding="utf-8"))
    except OSError:
        return ""
    value = fields.get("id", "")
    return value if isinstance(value, str) else ""


def _host_row(runs_dir: Path, host: str) -> dict:
    """Edge. The hosts-table row named `host`; an empty row when the table has none by that name."""
    return next((row for row in run_store.hosts(runs_dir) if str(row.get("name")) == host), {})


def _cli_run(runs_dir: Path, argv: list[str]) -> dict:
    """Edge. Runs `argv` through `store_cli.runner(runs_dir)` and parses its stdout as the row it printed;
    an unparseable or non-object reply reads as an empty row, never an exception."""
    _, output = store_cli.runner(runs_dir)(argv)
    try:
        parsed = json.loads(output)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _login_runner_and_env(profile: dict) -> tuple[str, tuple[str, ...]]:
    """A provider profile's check_login `runner` (its `runner` key, or today's default `"claude-code"`) and
    `env_names`: the tuple of whichever of `auth_env`, `endpoint_env` the profile sets, in that order,
    skipping an absent one."""
    runner = profile.get("runner") or "claude-code"
    env_names = tuple(v for v in (profile.get("auth_env"), profile.get("endpoint_env")) if v)
    return runner, env_names


def _check_login_edge(runs_dir: Path, ssh_run: Run, provider_profile: Callable[[], dict], host: str) -> dict:
    """Edge. `check_login_on_host` for `host`, its ssh and versions read from the hosts table, "now" read
    from the clock at call time -- never at `edge_deps` construction time. Its `runner` and `env_names`
    come from `provider_profile()`, resolved fresh on each call through `_login_runner_and_env`."""
    row = _host_row(runs_dir, host)
    ssh = str(row.get("ssh", ""))
    current_versions = chair_login_watch._versions(row)
    now = datetime.now(UTC).isoformat()
    runner, env_names = _login_runner_and_env(provider_profile())
    return chair_login_check.check_login_on_host(
        host, ssh, current_versions, now, ssh_run, partial(_cli_run, runs_dir), runner=runner, env_names=env_names,
    )


_CHECKS_TIMEOUT_S = 600.0
_CHECK_COMMAND_TIMEOUT_S = 900.0


def _must(code: int, output: str, what: str) -> str:
    if code != 0:
        raise RuntimeError(f"{what}: {output.strip()}")
    return output


class _GitEdge:
    """Edge. GitPort over one throwaway worktree of `repo`; the main checkout is never switched or written.

    Every base is `origin/<default branch>` after a `git fetch origin`; local `main` is never read, so it cannot be stale."""

    def __init__(self, runs_dir: Path, workspace: Path, repo: str) -> None:
        self.runs_dir, self.workspace, self.repo, self.tmp = runs_dir, workspace, repo, ""

    def _git(self, *args: str) -> tuple[int, str]:
        return run_argv(["git", "-C", self.tmp or self.repo, *args])

    def default_branch(self) -> str:
        """What `origin/HEAD` names; "main" when it names nothing."""
        code, out = run_argv(["git", "-C", self.repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        name = out.strip().removeprefix("origin/") if code == 0 else ""
        return name or "main"

    def fetch_branch(self, host: str, branch: str) -> None:
        """The run's branch from its host, reached as `<ssh>:<repo on the host>` the way `remote_fetch` reaches it. An empty host is here."""
        if not host:
            return
        lane = next(iter(host_cmd.host_rows_to_lane_hosts([{**_host_row(self.runs_dir, host), "state": "active"}])), None)
        if lane is None or not lane.workspace_dir:
            raise RuntimeError(f"host {host} has no ssh or workspace_dir recorded")
        host_repo = remote_fetch.host_repo_path(str(self.workspace), lane.workspace_dir, self.repo)
        _must(*self._git("fetch", f"{lane.ssh}:{host_repo}", f"+refs/heads/{branch}:refs/heads/{branch}"), f"git fetch {branch} from {host}")

    def create_branch_from_main(self, name: str) -> None:
        """Fetches origin, then cuts `name` from `origin/<default branch>` in a new worktree; a failed fetch raises."""
        _must(*run_argv(["git", "-C", self.repo, "fetch", "origin"]), "git fetch origin")
        start = f"origin/{self.default_branch()}"
        tmp = tempfile.mkdtemp(prefix="cox-carry-")
        code, out = run_argv(["git", "-C", self.repo, "worktree", "add", "-B", name, tmp, start])
        if code != 0:
            Path(tmp).rmdir()
            raise RuntimeError(f"git worktree add {name} from {start}: {out.strip()}")
        self.tmp = tmp

    def cherry_pick(self, commit: str) -> list[str]:
        code, out = self._git("cherry-pick", commit)
        if code == 0:
            return []
        conflicts = [line for line in self._git("diff", "--name-only", "--diff-filter=U")[1].splitlines() if line.strip()]
        self._git("cherry-pick", "--abort")
        if not conflicts:
            raise RuntimeError(f"git cherry-pick {commit}: {out.strip()}")
        return conflicts

    def push(self, branch: str) -> None:
        ok, detail = forge_auto.push(self.tmp, branch)
        if not ok:
            raise RuntimeError(f"push of {branch}: {detail}")

    def run_checks(self) -> list[str]:
        """The failing lines of the worktree's `.agent-checks`, each run through bash there; blanks and comments skipped."""
        try:
            lines = (Path(self.tmp) / ".agent-checks").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        commands = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
        return [c for c in commands if run_argv(["bash", "-c", c], cwd=Path(self.tmp), timeout=_CHECK_COMMAND_TIMEOUT_S)[0] != 0]

    def already_on_main(self, commit: str) -> bool:
        return self._git("merge-base", "--is-ancestor", commit, f"origin/{self.default_branch()}")[0] == 0

    def close(self) -> None:
        if self.tmp:
            run_argv(["git", "-C", self.repo, "worktree", "remove", "--force", self.tmp])


def _pr_url(detail: str) -> str:
    """The URL `gh pr create` prints as its last line; "" when the forge printed none, as the local forge does."""
    last = detail.strip().splitlines()[-1].strip() if detail.strip() else ""
    return last if last.startswith("http") else ""


class _ForgeEdge:
    """Edge. ForgePort over `forge_auto` in the git edge's worktree; the carry's PR body already carries the footer."""

    def __init__(self, git: _GitEdge) -> None:
        self.git, self.branch, self.pr_url = git, "", ""

    def open_pr(self, branch: str, title: str, body: str) -> str:
        ok, detail = forge_auto.open_pr(self.git.tmp, title, body, head=branch, base=self.git.default_branch())
        if not ok:
            raise RuntimeError(f"open PR for {branch}: {detail}")
        self.branch, self.pr_url = branch, _pr_url(detail)
        return self.pr_url or detail

    def wait_checks(self, pr: str) -> list[str]:
        ok, detail = forge_auto.wait_checks(self.git.tmp, _CHECKS_TIMEOUT_S)
        return [] if ok else [detail]

    def merge(self, pr: str) -> None:
        ok, detail = forge_auto.merge(self.git.tmp, {"branch": self.branch, "default_branch": self.git.default_branch()})
        if not ok:
            raise RuntimeError(f"merge of {self.branch}: {detail}")


class _StoreEdge:
    """Edge. StorePort over `store_cli`, then the ticket file, the way the review path closes a landed task."""

    def __init__(self, runs_dir: Path, work_dir: Path, action: Action, forge: _ForgeEdge, now: Callable[[], str]) -> None:
        self.runs_dir, self.work_dir, self.action, self.forge, self.now = runs_dir, work_dir, action, forge, now

    def mark_landed(self, task: str, run: str) -> None:
        """Stamps the PR url `open_pr` returned. With no PR (every pick already on main) there is nothing to stamp."""
        if not self.forge.pr_url:
            return
        result = store_cli.mark_landed(self.runs_dir, run, self.action["phase"], task, self.forge.pr_url, self.now())
        error = chair_apply_review._stamped(result)
        if error:
            raise RuntimeError(error)

    def set_done(self, task: str) -> None:
        initiative = self.action["initiative"]
        error = chair_apply_review._set_done(store_cli.set_state(self.runs_dir, initiative, task, "done", "chair"))
        if error:
            raise RuntimeError(error)
        path = _ticket_paths(self.work_dir, initiative).get(task)
        if path is not None:
            new_text, _ = approve_to_done(path.read_text(encoding="utf-8"), merged=True)
            if new_text is not None:
                path.write_text(new_text, encoding="utf-8")


class _RebaseEdge:
    """Edge. RebasePort over the remote: tips are read with ls-remote and pushes never check anything out."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _git(self, *args: str) -> tuple[int, str]:
        return run_argv(["git", "-C", self.repo, *args])

    def _remote_tip(self, ref: str) -> str | None:
        out = _must(*self._git("ls-remote", "origin", f"refs/heads/{ref}"), f"git ls-remote {ref}")
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if name.strip() == f"refs/heads/{ref}":
                return sha
        return None

    def branch_tip(self, branch: str) -> str | None:
        return self._remote_tip(branch)

    def create_ref(self, ref: str, sha: str) -> None:
        _must(*self._git("push", "origin", f"{sha}:refs/heads/{ref}"), f"git push backup {ref}")

    def recreate_branch(self, branch: str, base: str) -> None:
        """Fetches origin, then force-pushes `origin/<base>` (the tip the planner read, never local main) as the branch."""
        _must(*self._git("fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"), f"git fetch origin {base}")
        _must(*self._git("push", "--force", "origin", f"origin/{base}:refs/heads/{branch}"), f"git push --force {branch}")

    def ref_exists(self, ref: str) -> bool:
        return self._remote_tip(ref) is not None


def _carry_ports(
    runs_dir: Path, workspace: Path, repo_for: Callable[[Action], str], now: Callable[[], str], action: Action,
) -> tuple[GitPort, ForgePort, StorePort]:
    git = _GitEdge(runs_dir, workspace, action.get("repo") or repo_for(action))
    forge = _ForgeEdge(git)
    return git, forge, _StoreEdge(runs_dir, workspace, action, forge, now)


def edge_deps(
    runs_dir: Path,
    workspace: Path,
    session: str,
    pid: int,
    run_id: Callable[[Action], str],
    repo_for: Callable[[Action], str],
    record: Callable[[Action], None],
    host: str,
    harness_python: str,
    log_retention_days: int = 7,
    ids_mode: str = "slug",
    provider_profile: Callable[[], dict] = lambda: {},
    dry_run: bool = False,
) -> Deps:
    """Edge. The real bundle: subprocess for cox and git, chair.acquire_lease for the lease.

    A dry run drops every door to a lane host: `run` refuses an ssh or push argv unrun and `check_login` returns no row.

    Every subprocess runs in `workspace`, because `cox route launch epic` reads `work/<id>/initiative.md` from its cwd.
    A take_lease action names no holder, so the lease is always taken as this loop's own session, pid and host.
    `provider_profile` is the routing profile's `provider_profile` YAML, already resolved; unset, it names no
    runner override and `check_login` keeps today's `"claude-code"` default.
    """
    run = _local_only(workspace) if dry_run else _bounded_for_ssh(workspace)
    live_check = partial(_check_login_edge, runs_dir, partial(run_lane_host, cwd=workspace), provider_profile)
    return Deps(
        run=run,
        delete_branches=lambda repo, pattern: delete_branches_with(run, repo, pattern),
        acquire_lease=lambda _holder, _host, steal=False: chair.acquire_lease(runs_dir, session, pid, host, steal=steal),
        record=record,
        run_id=run_id,
        repo_for=repo_for,
        ssh_for=lambda name: str(_host_row(runs_dir, name).get("ssh", "")),
        intake_id=partial(read_intake_id, workspace),
        runs_dir=runs_dir,
        work_dir=workspace,
        check_login=(lambda name: {}) if dry_run else live_check,
        log_retention_days=log_retention_days,
        harness_python=harness_python,
        ids_mode=ids_mode,
        carry_ports=partial(_carry_ports, runs_dir, workspace, repo_for, lambda: datetime.now(UTC).isoformat()),
        rebase_port=lambda action: _RebaseEdge(action.get("repo") or repo_for(action)),
    )
