"""Edge: bring an ended remote run's records and branches back to the chair. `run_cmd` takes an argv and returns its exit code.

Arguments
- `host`: the `LaneHost`; its `workspace_dir` holds `runs/<run>/` and `runs/<run>.log` on the host.
- `chair_runs_dir`: the chair's runs directory; the chair workspace is its parent.
- `repo_paths`: reads repo paths out of the fetched run directory.
- `locate`: turns a host path into an rsync or git location; the default is `<ssh>:<path>`.

Where the repos come from. `cox runs land` does not read a repo from a record; it
takes `--repo` on the command line. The only record field that names a task's
repo is the optional `repo` key of `tasks/<phase>/<task>.json`, which
`runs_stranded._remedy` reads before falling back to the work item. `task_repos`
reads that key, so there is no new schema. A caller that knows the repo the way
land does passes `lambda _: [repo]` instead. The key is optional, so a run whose
records name no repo is a `FetchError`, never an empty success.

Repo path rule. A recorded path may be a host path or a chair path, because the
lane writes the path it saw. A path under the host `workspace_dir` maps to the
same relative path under the chair workspace. The chair path then maps back by
swapping the chair workspace prefix for the host `workspace_dir`. A path outside
both workspaces is the same path on both machines. Matching is on a path
component, so `/ws-other` is not under `/ws`.

Each repo is fetched with `git -C <chair repo> fetch <host repo> <refspec>`. Then
`git -C <chair repo> ls-remote --exit-code . refs/heads/agents/<run>/*` must find
a ref, so a fetch that brought no branch is a `FetchError`. A lane whose lease is
held or that has no `ended_at` is refused before anything is copied.

A run that stopped before writing anything never created `runs/<run>/` on the
host, so rsyncing it first would exit 23 every tick forever. `fetch_run` probes
for the run directory and the log first, cheaply, over the same rsync
connection, then hands the two booleans to `fetch_plan` (mirroring the
`launch_plan`/`launch_on_host` split in `remote_launch.py`) to decide, purely,
which steps to run."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_tools.land import _approved as land_approved
from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import git_fetch_argv, rsync_pull_argv

__all__ = [
    "FetchError", "FetchPlan", "approved_without_branch", "chair_repo_path", "fetch_plan", "fetch_run", "host_repo_path", "probe_argv",
    "pull_argvs", "refuse_unended", "task_repos",
]


@dataclass(frozen=True)
class FetchError:
    step: str
    message: str


@dataclass(frozen=True)
class FetchPlan:
    pull_argvs: list[list[str]]
    do_git_fetch: bool
    outcome: tuple[str, ...] | None


def refuse_unended(lease_released: bool, ended_at: str | None) -> str | None:
    if lease_released and ended_at:
        return None
    return "the lane has not ended: its lease is held or it has no ended_at"


def _swap(from_prefix: str, to_prefix: str, path: str) -> str:
    old = from_prefix.rstrip("/")
    if path == old or path.startswith(old + "/"):
        return to_prefix.rstrip("/") + path[len(old):]
    return path


def host_repo_path(chair_workspace: str, host_workspace: str, chair_repo: str) -> str:
    return _swap(chair_workspace, host_workspace, chair_repo)


def chair_repo_path(chair_workspace: str, host_workspace: str, recorded: str) -> str:
    return _swap(host_workspace, chair_workspace, recorded)


def pull_argvs(run_location: str, log_location: str, chair_runs_dir: str, run: str) -> list[list[str]]:
    """The run directory lands at `<chair_runs_dir>/<run>/`, the log beside it."""
    runs = chair_runs_dir.rstrip("/")
    return [
        rsync_pull_argv(run_location.rstrip("/") + "/", f"{runs}/{run}/"),
        rsync_pull_argv(log_location, runs + "/"),
    ]


def probe_argv(location: str) -> list[str]:
    """A bounded `rsync --list-only` of one location; the `.` destination is required by the builder and never written."""
    argv = rsync_pull_argv(location, ".")
    return [argv[0], "--list-only", *argv[1:]]


def fetch_plan(
    run_location: str,
    log_location: str,
    chair_runs_dir: str,
    run: str,
    run_dir_exists: bool,
    log_exists: bool,
) -> FetchPlan:
    """The rsync argvs `fetch_run` runs, whether it goes on to git-fetch, and the outcome to return
    if it should stop right there. A run directory that exists is pulled in full, as always.

    One that never existed but left a log is a run that ended before writing a single task record:
    there is nothing to git-fetch or verify, so the log is pulled alone, `runs/<run>/tasks/` is made
    empty, and the outcome is `("fetched: no tasks ran",)` rather than a `FetchError`. A run with
    neither directory nor log falls through to the full plan, which fails exactly as it does today."""
    if not run_dir_exists and log_exists:
        runs = chair_runs_dir.rstrip("/")
        return FetchPlan([rsync_pull_argv(log_location, runs + "/")], False, ("fetched: no tasks ran",))
    return FetchPlan(pull_argvs(run_location, log_location, chair_runs_dir, run), True, None)


def _repo_of(path: Path) -> str | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    repo = record.get("repo") if isinstance(record, dict) else None
    return repo if isinstance(repo, str) and repo else None


def task_repos(run_dir: Path) -> list[str]:
    """Distinct `repo` values of the task records, in path order."""
    found = (_repo_of(p) for p in sorted(run_dir.glob("tasks/*/*.json")))
    return list(dict.fromkeys(r for r in found if r is not None))


def approved_without_branch(records: Sequence[dict[str, Any]], branches: frozenset[str], run: str) -> list[str]:
    """Ids (`task` key) of records `land` calls approved whose `agents/<run>/<task>` is not in `branches`."""
    return [
        str(r["task"]) for r in records
        if land_approved(r) is None and f"agents/{run}/{r['task']}" not in branches
    ]


def _task_records(run_dir: Path) -> list[dict[str, Any]] | None:
    """The run's task records, or None when any is unreadable, has no `task` id, or none exist."""
    records: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("tasks/*/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or not isinstance(record.get("task"), str):
            return None
        records.append(record)
    return records or None


def _records_in(records: list[dict[str, Any]], repo: str, chair_ws: str, host_ws: str) -> list[dict[str, Any]]:
    """The records that name `repo`, or name no repo."""
    return [r for r in records if not r.get("repo") or chair_repo_path(chair_ws, host_ws, str(r["repo"])) == repo]


def fetch_run(
    host: LaneHost,
    run: str,
    chair_runs_dir: Path,
    repo_paths: Callable[[Path], Sequence[str]],
    run_cmd: Callable[[list[str]], int],
    locate: Callable[[str], str] | None = None,
    *,
    lease_released: bool,
    ended_at: str | None,
) -> tuple[str, ...] | FetchError:
    """The chair repos whose `agents/<run>/*` branches now exist, or the step that failed. Nothing is raised."""
    refusal = refuse_unended(lease_released, ended_at)
    if refusal is not None:
        return FetchError("refuse", refusal)
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    host_ws = host.workspace_dir.rstrip("/")
    run_location = place(f"{host_ws}/runs/{run}")
    log_location = place(f"{host_ws}/runs/{run}.log")
    # A cheap probe over the same connection rsync uses, so a run directory that never existed
    # (the run stopped before writing anything) is planned for, instead of rsynced blind and retried
    # forever on its exit-23 "No such file or directory".
    run_dir_exists = run_cmd(probe_argv(run_location)) == 0
    log_exists = run_cmd(probe_argv(log_location)) == 0
    chair_runs_dir.mkdir(parents=True, exist_ok=True)
    plan = fetch_plan(run_location, log_location, str(chair_runs_dir), run, run_dir_exists, log_exists)
    for argv in plan.pull_argvs:
        code = run_cmd(argv)
        if code != 0:
            return FetchError("rsync", f"{' '.join(argv)} exited {code}")
    if plan.outcome is not None:
        (chair_runs_dir / run / "tasks").mkdir(parents=True, exist_ok=True)
        return plan.outcome
    chair_ws = str(chair_runs_dir.parent)
    repos = tuple(dict.fromkeys(chair_repo_path(chair_ws, host_ws, r) for r in repo_paths(chair_runs_dir / run)))
    if not repos:
        return FetchError("repos", f"no task record under {chair_runs_dir / run}/tasks names a repo")
    records = _task_records(chair_runs_dir / run)
    for repo in repos:
        # git_fetch_argv has no repo selector, so `-C` goes in after its leading "git".
        # git runs its own ssh transport, so no 30s bound from this file applies to this call.
        fetch = git_fetch_argv(place(host_repo_path(chair_ws, host_ws, repo)), run)
        code = run_cmd(["git", "-C", repo, *fetch[1:]])
        if code != 0:
            return FetchError("git", f"fetching {run} into {repo} exited {code}")
        scoped = _records_in(records, repo, chair_ws, host_ws) if records is not None else []
        if scoped:
            # Readable records decide: a run with no approved task has no branch by design.
            # run_cmd returns only an exit code, so each task's branch is probed on its own.
            arrived = frozenset(
                name for name in (f"agents/{run}/{r['task']}" for r in scoped)
                if run_cmd(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"]) == 0
            )
            missing = approved_without_branch(scoped, arrived, run)
            if missing:
                names = ", ".join(missing)
                return FetchError("verify", f"approved task(s) {names} have no refs/heads/agents/{run}/<task> in {repo} after the fetch")
            continue
        found = run_cmd(["git", "-C", repo, "ls-remote", "--exit-code", ".", f"refs/heads/agents/{run}/*"])
        if found != 0:
            return FetchError("verify", f"no refs/heads/agents/{run}/* in {repo} after the fetch")
    return repos
