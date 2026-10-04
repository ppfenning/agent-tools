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
    chair_housekeeping,
    chair_login_check,
    chair_login_watch,
    chair_plan_prune,
    courier,
    remote_lane,
    route,
    run_store,
    stale_draft,
    store_cli,
)
from agent_tools.chair_facts import STRANDED_CAUSE
from agent_tools.chair_types import Action, LandTrigger, is_fenced
from agent_tools.remote_argv import ssh_argv, sync_argv

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
    "perform",
    "smoke_targets",
    "tail",
]

Status = Literal[
    "fenced", "dry_run", "skipped", "refused", "recorded", "done", "failed", "landed", "not_landed", "escalated", "busy",
    "in_progress",
]

Run = Callable[[list[str]], tuple[int, str]]


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


LAUNCH_KINDS = ("relaunch", "retry", "launch_epic", "launch_decompose", "rescue")
_UNFENCED = ("standby", "take_lease")  # not writes, so a stale or missing epoch does not stop them
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
        return [
            "cox", "route", "launch", "decompose", "--idea", idea, "--initiative-id", initiative_id, *task_ids, "--no-claim",
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
    refusal = land_refusal(action, code, output)
    if refusal is not None:
        return {"action": action, "status": "refused", "reason": output, "needs_chair": refusal}
    if _repo_busy(output):
        return _result(action, "busy", output)
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
    if at_home(verify)[0] == 0:
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
    refusal = land_refusal(action, code, output)
    if refusal is not None:
        return {"action": action, "status": "refused", "reason": output, "needs_chair": refusal}
    if _repo_busy(output):
        return _result(action, "busy", output)
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


def _send_stale_draft_note(deps: Deps, initiative: str, reason: str) -> None:
    """Edge. One `coxswain://initiative/<id>` courier line appended to `work_dir/courier.jsonl`, addressed to
    `deps.note_to`, quoting `reason` verbatim."""
    ref = courier.Reference("initiative", initiative)
    sender = (chair.read(deps.runs_dir) or {}).get("session") or "chair"
    entry = courier.send(ref, sender, deps.note_to, reason, uuid.uuid4().hex)
    path = deps.work_dir / "courier.jsonl"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(courier.append_line(existing, entry), encoding="utf-8")


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
    if kind == "take_lease":
        return _lease(action, deps)
    if kind == "check_login":
        return _check_login(action, deps)
    if kind in ("standby", "needs_chair", "mark_lost"):
        return _result(action, "recorded")
    if kind == "fetch_exit":
        return _fetch_exit(action, deps)
    if kind in LAUNCH_KINDS or kind in ("pull", "fetch"):
        return _launch(action, deps)
    if kind == "housekeeping":
        return _housekeeping(action, deps)
    if kind == "stale_to_draft":
        return _stale_to_draft(action, deps)
    if kind == "stalled_usr1":
        return _stalled(action, deps, signal.SIGUSR1)
    if kind == "stalled_stop":
        return _stalled(action, deps, signal.SIGTERM)
    return _result(action, "refused", f"unsupported action kind {kind!r}")


def perform(actions: list[Action], deps: Deps, current_epoch: Callable[[], int], dry_run: bool) -> list[Result]:
    """Edge. One result per action, in order; each is recorded after it runs.

    The epoch is re-read per action. standby and take_lease are never fenced. A dry run touches and records nothing.
    A relaunch whose initiative had a clear_branches end other than done earlier this tick is skipped.
    A land or land_phase whose command exits nonzero is refused, escalated with the cause `land_refusal` read
    from its output, and blocks its repo's later lands and land_phases this tick. A land refused before its
    command runs blocks nothing.
    With a land sink, a land returns `in_progress` at once and the actions after it run in the same tick. The lands
    that finished since the last tick are collected first, so their results lead this tick's.
    """
    results: list[Result] = []
    blocked: dict[str, str] = {}  # repo -> task or phase of the uncounted land or land_phase that blocks its later lands and deletes
    uncleared: dict[str, str] = {}  # initiative -> status of its clear_branches that did not finish done, this tick
    for finished in deps.lands.collect() if deps.lands is not None and not dry_run else []:
        land = finished["action"]
        if finished["status"] == "not_landed" or "needs_chair" in finished:
            blocked[land.get("repo", "")] = _land_subject(land)
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
        else:
            result = _execute(action, deps, blocked)
        if action.get("kind") in ("land", "land_phase") and (result["status"] == "not_landed" or "needs_chair" in result):
            blocked[action.get("repo", "")] = _land_subject(action)
        if action.get("kind") == "clear_branches" and result["status"] != "done":
            uncleared[initiative] = result["status"]
        if result["status"] != "in_progress":  # the land's own result is recorded when it is collected
            deps.record(_recorded(result))
        results.append(result)
        if action.get("kind") in ("land", "land_phase") and result["status"] not in ("landed", "fenced", "busy", "in_progress"):
            escalated = _escalate(action, result)
            deps.record(_recorded(escalated))
            results.append(escalated)
        if action.get("kind") == "clear_branches" and "needs_chair" in result:
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


_LOGIN_CHECK_TIMEOUT_S = 60  # an unreachable lane host must not stall a tick


def run_argv(argv: list[str], cwd: Path | None = None, timeout: float | None = None) -> tuple[int, str]:
    """Edge. A missing binary is exit 127 and a timeout exit 124, each with its message, never an exception out of perform."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd, timeout=timeout)
    except OSError as error:
        return 127, f"{argv[0] if argv else '<empty argv>'}: {error}"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0] if argv else '<empty argv>'}: timed out after {timeout}s"
    return done.returncode, done.stdout + done.stderr


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
) -> Deps:
    """Edge. The real bundle: subprocess for cox and git, chair.acquire_lease for the lease.

    Every subprocess runs in `workspace`, because `cox route launch epic` reads `work/<id>/initiative.md` from its cwd.
    A take_lease action names no holder, so the lease is always taken as this loop's own session, pid and host.
    `provider_profile` is the routing profile's `provider_profile` YAML, already resolved; unset, it names no
    runner override and `check_login` keeps today's `"claude-code"` default.
    """
    run = partial(run_argv, cwd=workspace)
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
        check_login=partial(
            _check_login_edge, runs_dir, partial(run_argv, cwd=workspace, timeout=_LOGIN_CHECK_TIMEOUT_S), provider_profile,
        ),
        log_retention_days=log_retention_days,
        harness_python=harness_python,
        ids_mode=ids_mode,
    )
