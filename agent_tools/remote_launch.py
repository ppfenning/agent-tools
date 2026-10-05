"""Edge: copy an initiative to a lane host, then start the lane there. `run` takes an argv and returns its exit code."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import launch_argv, rsync_push_argv, ssh_argv, sync_argv
from agent_tools.remote_doctor import env_verdict
from agent_tools.remote_lane import remote_record

__all__ = ["LaunchError", "env_preflight", "launch_on_host", "launch_plan"]


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
    harness_dir: str | None = None,
) -> list[list[str]]:
    """The argv that `launch_on_host` runs in order: the rsync push, then, when `harness_dir` is
    given, the sync of the harness on the lane host, then, when `repo` is given, the sync of `repo`
    there, then the launch ssh command.

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
    return [rsync_step, *harness_steps, *repo_steps, launch_step]


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
    *,
    preflight: str | None = None,
) -> dict | LaunchError:
    """`preflight` is a refusal line from a check made before anything is copied; None lets the launch go on."""
    if preflight is not None:
        return LaunchError("auth", preflight)
    plan = launch_plan(host, initiative, run_id, label, locate, repo, harness_dir)
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
        if synced != 0:
            return LaunchError(
                "sync", f"updating {repo} on {host.name} exited {synced}: the lane would build on a stale main"
            )
    started = run(ssh_cmd)
    if started != 0:
        return LaunchError("ssh", f"starting the lane on {host.name} exited {started}")
    return remote_record(host.name, launched_at, repo)
