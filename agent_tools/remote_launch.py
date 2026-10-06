"""Edge: copy an initiative to a lane host, then start the lane there. `run` takes an argv and returns its exit code."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import (
    agent_refspec,
    branch_push_argv,
    epic_refspec,
    launch_argv,
    rsync_push_argv,
    ssh_argv,
    sync_argv,
)
from agent_tools.remote_doctor import env_verdict
from agent_tools.remote_lane import remote_record

__all__ = ["LaunchError", "Relaunch", "env_preflight", "launch_on_host", "launch_plan"]


@dataclass(frozen=True)
class LaunchError:
    step: str
    message: str


@dataclass(frozen=True)
class Relaunch:
    """A relocated relaunch: `tasks` are the approved task ids whose branches `prior_run` left under agents/<prior_run>/."""

    prior_run: str
    tasks: tuple[str, ...] = ()
    local_repo: str | None = None  # None: the chair's checkout is at the same path as the lane host's `repo`


def _relaunch_branches(initiative: str, relaunch: Relaunch) -> list[tuple[str, str]]:
    """(branch, refspec) pairs, the epic glob first, then one per task in `relaunch.tasks` order."""
    return [
        (f"epic/{initiative}/*", epic_refspec(initiative)),
        *((f"agents/{relaunch.prior_run}/{task}", agent_refspec(relaunch.prior_run, task)) for task in relaunch.tasks),
    ]


def launch_plan(
    host: LaneHost,
    initiative: str,
    run_id: str,
    label: str,
    locate: Callable[[str], str] | None = None,
    repo: str | None = None,
    harness_dir: str | None = None,
    relaunch: Relaunch | None = None,
) -> list[list[str]]:
    """The argv that `launch_on_host` runs in order: the rsync push, then, when `harness_dir` is
    given, the sync of the harness on the lane host, then, when `repo` is given, the sync of `repo`
    there, then, for a `relaunch` with a `repo`, a force push of the epic branches and of each task
    branch of `relaunch.prior_run` to the lane host, then the launch ssh command.

    rsync needs the parent of the destination to exist; the copy keeps the local work/<id> layout.
    `route launch` reads --initiative as a path from its cwd, and an ssh command starts in the home directory.
    `repo` is the same path on both machines (each keeps it under ~/repos/<name>), so it is passed
    through unchanged rather than rewritten under host.workspace_dir; so is `harness_dir`."""
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    src = f"work/{initiative}"
    remote_dir = f"{host.workspace_dir.rstrip('/')}/{src}"
    # The local work/<initiative> is the source: a ticket moved or dropped here must not linger on the lane host,
    # where a second copy of one id fails the run's DAG check.
    rsync_step = rsync_push_argv(src, place(remote_dir), mirror=True)
    launch_step = ssh_argv(host.ssh, launch_argv(remote_dir, run_id, label))
    harness_steps = [] if harness_dir is None else [ssh_argv(host.ssh, sync_argv(harness_dir))]
    repo_steps = [] if repo is None else [ssh_argv(host.ssh, sync_argv(repo))]
    push_steps = (
        []
        if relaunch is None or repo is None
        else [
            branch_push_argv(repo if relaunch.local_repo is None else relaunch.local_repo, host.ssh, repo, refspec)
            for _, refspec in _relaunch_branches(initiative, relaunch)
        ]
    )
    return [rsync_step, *harness_steps, *repo_steps, *push_steps, launch_step]


def env_preflight(names: tuple[str, ...], probes: Mapping[str, tuple[int, str]]) -> str | None:
    """None when every name's probe (printenv exit code, text) is a non-empty value. Exit 1 is unset; any other non-zero is ssh failing."""
    if not names:
        return "no auth_env or endpoint_env configured: nothing to check on the host"
    broken = next((probes[name][1] for name in names if probes[name][0] not in (0, 1)), None)
    if broken is not None:
        return f"env var check failed on the host: {broken.strip()[:80]}"
    verdict = env_verdict(names, lambda name: probes[name][1] if probes[name][0] == 0 else "")
    return None if verdict is None else f"{verdict} (set it there)"


def launch_on_host(
    host: LaneHost,
    initiative: str,
    run_id: str,
    label: str,
    launched_at: str,
    run: Callable[[list[str]], int],
    locate: Callable[[str], str] | None = None,
    repo: str | None = None,
    harness_dir: str | None = None,
    relaunch: Relaunch | None = None,
    *,
    preflight: str | None = None,
) -> dict | LaunchError:
    """`preflight` is a refusal line from a check made before anything is copied; None lets the launch go on."""
    if preflight is not None:
        return LaunchError("auth", preflight)
    if relaunch is not None and repo is None:
        return LaunchError(
            "push", f"a relocated relaunch to {host.name} has no repo to push epic/{initiative}/* into: nothing was run"
        )
    plan = launch_plan(host, initiative, run_id, label, locate, repo, harness_dir, relaunch)
    rsync_argv, ssh_cmd = plan[0], plan[-1]
    pushed = run(rsync_argv)
    if pushed != 0:
        return LaunchError("rsync", f"rsync of work/{initiative} to {host.name} exited {pushed}")
    next_step = 1
    if harness_dir is not None:
        updated = run(plan[next_step])
        next_step += 1
        if updated != 0:
            return LaunchError(
                "harness",
                f"updating the harness at {harness_dir} on {host.name} exited {updated}: "
                "the lane would run an older harness than the store",
            )
    if repo is not None:
        synced = run(plan[next_step])
        next_step += 1
        if synced != 0:
            return LaunchError(
                "sync", f"updating {repo} on {host.name} exited {synced}: the lane would build on a stale main"
            )
    branches = [] if relaunch is None else [branch for branch, _ in _relaunch_branches(initiative, relaunch)]
    for branch, push_step in zip(branches, plan[next_step:-1], strict=True):
        pushed_branch = run(push_step)
        if pushed_branch != 0:
            return LaunchError(
                "push",
                f"pushing {branch} to {host.name} exited {pushed_branch}: the lane would not have the initiative's branches",
            )
    started = run(ssh_cmd)
    if started != 0:
        return LaunchError("ssh", f"starting the lane on {host.name} exited {started}")
    return remote_record(host.name, launched_at, repo)
