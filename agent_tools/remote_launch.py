"""Edge: copy an initiative to a lane host, then start the lane there. `run` takes an argv and returns its exit code."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import launch_argv, rsync_push_argv, ssh_argv, sync_argv
from agent_tools.remote_lane import remote_record

__all__ = ["LaunchError", "launch_on_host", "launch_plan"]


@dataclass(frozen=True)
class LaunchError:
    step: str
    message: str


def launch_plan(
    host: LaneHost,
    initiative: str,
    run_id: str,
    label: str,
    locate: Callable[[str], str] | None = None,
    repo: str | None = None,
) -> list[list[str]]:
    """The argv that `launch_on_host` runs in order: the rsync push, then, when `repo` is
    given, the sync of `repo` on the lane host, then the launch ssh command.

    rsync needs the parent of the destination to exist; the copy keeps the local work/<id> layout.
    `route launch` reads --initiative as a path from its cwd, and an ssh command starts in the home directory.
    `repo` is the same path on both machines (each keeps it under ~/repos/<name>), so it is passed
    through unchanged rather than rewritten under host.workspace_dir."""
    place = locate if locate is not None else (lambda path: f"{host.ssh}:{path}")
    src = f"work/{initiative}"
    remote_dir = f"{host.workspace_dir.rstrip('/')}/{src}"
    rsync_step = rsync_push_argv(src, place(remote_dir))
    launch_step = ssh_argv(host.ssh, launch_argv(remote_dir, run_id, label))
    if repo is None:
        return [rsync_step, launch_step]
    return [rsync_step, ssh_argv(host.ssh, sync_argv(repo)), launch_step]


def launch_on_host(
    host: LaneHost,
    initiative: str,
    run_id: str,
    label: str,
    launched_at: str,
    run: Callable[[list[str]], int],
    locate: Callable[[str], str] | None = None,
    repo: str | None = None,
    *,
    preflight: str | None = None,
) -> dict | LaunchError:
    """`preflight` is a refusal line from a check made before anything is copied; None lets the launch go on."""
    if preflight is not None:
        return LaunchError("auth", preflight)
    plan = launch_plan(host, initiative, run_id, label, locate, repo)
    rsync_argv, ssh_cmd = plan[0], plan[-1]
    pushed = run(rsync_argv)
    if pushed != 0:
        return LaunchError("rsync", f"rsync of work/{initiative} to {host.name} exited {pushed}")
    if repo is not None:
        synced = run(plan[1])
        if synced != 0:
            return LaunchError(
                "sync", f"updating {repo} on {host.name} exited {synced}: the lane would build on a stale main"
            )
    started = run(ssh_cmd)
    if started != 0:
        return LaunchError("ssh", f"starting the lane on {host.name} exited {started}")
    return remote_record(host.name, launched_at, repo)
