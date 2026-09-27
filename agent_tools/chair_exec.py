"""The thin edge of a chair tick: perform planned actions through injected callables.

Pure helpers decide what an action means; `perform` only fences, dispatches and collects results.
Argv spellings follow `cox runs land --help` and `cox route launch epic|decompose|rescue --help`.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from agent_tools import chair, chair_apply_fetch, chair_housekeeping, chair_plan_prune, remote_lane, route, run_store
from agent_tools.chair_facts import STRANDED_CAUSE
from agent_tools.chair_types import Action, is_fenced

__all__ = [
    "LAUNCH_KINDS", "Deps", "Refusal", "Result", "argv_for", "branch_pattern", "decompose_id", "delete_branches_with", "edge_deps",
    "escalation", "land_refusal", "landed", "perform", "tail",
]

Status = Literal[
    "fenced", "dry_run", "skipped", "refused", "recorded", "done", "failed", "landed", "not_landed", "escalated", "busy",
]

Run = Callable[[list[str]], tuple[int, str]]


class Result(TypedDict):
    action: Action
    status: Status
    reason: str
    run: NotRequired[str]  # a fetch_exit's run, for the next task's status line
    host: NotRequired[str]  # a fetch_exit's remote host, read from its <run>.remote.json
    needs_chair: NotRequired[Action]  # a refused land's classified needs_chair, escalated in place of the generic one


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
    intake_id: Callable[[str], str] = _no_intake_id  # an intake path -> the id in its frontmatter; "" when it has none
    runs_dir: Path = field(default_factory=lambda: Path("."))  # a fetch_exit's task records and remote record live here
    work_dir: Path = field(default_factory=lambda: Path("."))  # a fetch_exit's ticket files live under work_dir/work/<initiative>


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
        "cause": cause, "epoch": action.get("epoch", 0),
    }


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


def argv_for(action: Action, initiative_id: str = "") -> list[str] | None:
    """The cox argv for a land, fetch, launch or pull action; None for any other kind or a missing required field.

    A decompose launch needs `initiative_id`, which `decompose_id` derives from the intake; it is never taken from the path.
    """
    kind = action.get("kind")
    task, repo, initiative = action.get("task_id", ""), action.get("repo", ""), action.get("initiative", "")
    run = action.get("run", "")
    idea = (action.get("intake_ids") or [""])[0]
    if kind == "land":
        return ["cox", "runs", "land", run, "--task", task, "--repo", repo, "--apply"] if run and task and repo else None
    if kind in ("fetch", "fetch_exit"):
        return ["cox", "runs", "fetch", run] if run else None
    if kind == "launch_decompose":
        return ["cox", "route", "launch", "decompose", "--idea", idea, "--initiative-id", initiative_id] if idea and initiative_id else None
    if kind == "rescue":
        return ["cox", "route", "launch", "rescue", "--initiative", f"work/{initiative}", "--task", task] if initiative and task else None
    if kind in LAUNCH_KINDS:
        host = action.get("host", "")
        return [
            "cox", "route", "launch", "epic", "--initiative", f"work/{initiative}",
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
    return _result(action, "landed" if landed(code, output) else "not_landed", output)


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
    code, listing = deps.run(["git", "-C", repo, "worktree", "list", "--porcelain"])
    if code != 0:
        return _result(action, "failed", f"git worktree list in {repo}: {listing.strip()}")
    entries = chair_plan_prune.worktrees_to_prune(listing, initiative)
    for argv in chair_plan_prune.prune_argv(entries):
        code, output = deps.run(["git", "-C", repo, *argv[1:]])
        if code != 0:
            return _result(action, "failed", f"{' '.join(argv)} in {repo}: {output.strip()}")
    # The prune already deleted its entries' branches, so the sweep finding nothing after a prune is success, not a miss.
    swept, error = deps.delete_branches(repo, pattern)
    deleted = [e["branch"] for e in entries] + swept
    if error:
        return _result(action, "failed", f"deleted {deleted} in {repo}; git: {error.strip()}")
    if not deleted:
        # Nothing stale is the state a clear exists to reach: a relaunch that failed after an earlier clear must not be
        # skipped forever because that clear already deleted the branches.
        return _result(action, "done", f"nothing to clear: no branch matched {pattern} in {repo}")
    return _result(action, "done", f"deleted {deleted} in {repo}")


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
    argv = argv_for(action, initiative_id)
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


def _prune_available(run: Run) -> bool:
    """Edge. True only when `python -m harness.store_backfill_traces prune --help` exits 0."""
    code, _ = run(["python", "-m", "harness.store_backfill_traces", "prune", "--help"])
    return code == 0


def _housekeeping(action: Action, deps: Deps) -> Result:
    """Lake sync, trace prune, runs clean, in order; the reason names all three, none stopping the others."""
    traces_root = run_store._traces_root(deps.runs_dir).url
    status, reason = chair_housekeeping.run_housekeeping(deps.run, traces_root, partial(_prune_available, deps.run))
    return _result(action, status, reason)


def _execute(action: Action, deps: Deps, blocked: dict[str, str]) -> Result:
    kind = action.get("kind")
    if kind == "land":
        return _land(action, deps, blocked)
    if kind == "clear_branches":
        return _clear(action, deps, blocked)
    if kind == "take_lease":
        return _lease(action, deps)
    if kind in ("standby", "needs_chair"):
        return _result(action, "recorded")
    if kind == "fetch_exit":
        return _fetch_exit(action, deps)
    if kind in LAUNCH_KINDS or kind in ("pull", "fetch"):
        return _launch(action, deps)
    if kind == "housekeeping":
        return _housekeeping(action, deps)
    return _result(action, "refused", f"unsupported action kind {kind!r}")


def perform(actions: list[Action], deps: Deps, current_epoch: Callable[[], int], dry_run: bool) -> list[Result]:
    """Edge. One result per action, in order; each is recorded after it runs.

    The epoch is re-read per action. standby and take_lease are never fenced. A dry run touches and records nothing.
    A relaunch whose initiative had a clear_branches end other than done earlier this tick is skipped.
    A land whose command exits nonzero is refused, escalated with the cause `land_refusal` read from its output,
    and blocks its repo's later lands this tick. A land refused before its command runs blocks nothing.
    """
    results: list[Result] = []
    blocked: dict[str, str] = {}  # repo -> task of the uncounted land that blocks its later lands and deletes
    uncleared: dict[str, str] = {}  # initiative -> status of its clear_branches that did not finish done, this tick
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
        if action.get("kind") == "land" and (result["status"] == "not_landed" or "needs_chair" in result):
            blocked[action.get("repo", "")] = action.get("task_id", "")
        if action.get("kind") == "clear_branches" and result["status"] != "done":
            uncleared[initiative] = result["status"]
        deps.record(_recorded(result))
        results.append(result)
        if action.get("kind") == "land" and result["status"] not in ("landed", "fenced", "busy"):
            escalated = _escalate(action, result)
            deps.record(_recorded(escalated))
            results.append(escalated)
    return results


def escalation(land: Action) -> Action:
    """The needs_chair a land that did not land raises when its command gave no refusal to classify: the facts
    dropped its stranded row, so this reports it instead."""
    return {
        "kind": "needs_chair", "initiative": land.get("initiative", ""), "task_id": land.get("task_id", ""),
        "cause": STRANDED_CAUSE, "epoch": land.get("epoch", 0),
    }


def _escalate(land: Action, result: Result) -> Result:
    raised = result.get("needs_chair") or escalation(land)
    return _result(raised, "escalated", f"land {land.get('task_id', '')} {result['status']}")


def run_argv(argv: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Edge. A missing binary is exit 127 with its message, never an exception out of perform."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd)
    except OSError as error:
        return 127, f"{argv[0] if argv else '<empty argv>'}: {error}"
    return done.returncode, done.stdout + done.stderr


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


def edge_deps(
    runs_dir: Path,
    workspace: Path,
    session: str,
    pid: int,
    run_id: Callable[[Action], str],
    repo_for: Callable[[Action], str],
    record: Callable[[Action], None],
) -> Deps:
    """Edge. The real bundle: subprocess for cox and git, chair.acquire_lease for the lease.

    Every subprocess runs in `workspace`, because `cox route launch epic` reads `work/<id>/initiative.md` from its cwd.
    """
    run = partial(run_argv, cwd=workspace)
    return Deps(
        run=run,
        delete_branches=lambda repo, pattern: delete_branches_with(run, repo, pattern),
        acquire_lease=lambda holder, host, steal=False: chair.acquire_lease(runs_dir, session, pid, host, steal=steal),
        record=record,
        run_id=run_id,
        repo_for=repo_for,
        intake_id=partial(read_intake_id, workspace),
        runs_dir=runs_dir,
        work_dir=workspace,
    )
