"""Edge: run `cox setup doctor` on a lane host over ssh; the caller prints what comes back."""

import json
from collections.abc import Callable

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import auth_status_argv, doctor_argv, env_check_argv, ssh_argv


def auth_verdict(output: str) -> str | None:
    """None when `claude auth status` output is JSON with `loggedIn` true; otherwise the refusal line."""
    try:
        status = json.loads(output)
    except ValueError:
        return f"claude auth: status unreadable: {output.strip()[:80]}"
    if not isinstance(status, dict):
        return f"claude auth: status unreadable: {output.strip()[:80]}"
    if status.get("loggedIn") is True:
        return None
    return "claude auth: not logged in on the host (run claude auth login there)"


def env_verdict(names: tuple[str, ...], run_one: Callable[[str], str]) -> str | None:
    """None when every name in `names` is non-empty on the host; otherwise names the first missing one."""
    for name in names:
        if not run_one(name).strip():
            return f"env vars: {name} is not set on the host"
    return None


def doctor_on_host(
    host: LaneHost,
    run: Callable[[list[str]], tuple[int, str]],
    runner: str = "claude-code",
    env_names: tuple[str, ...] = (),
) -> tuple[int, list[str]]:
    """`run` takes an argv and returns (exit code, combined output); result is (exit code, output rows)."""
    code, output = run(ssh_argv(host.ssh, doctor_argv()))
    if runner == "claude-code":
        _, auth_output = run(ssh_argv(host.ssh, auth_status_argv()))
        verdict = auth_verdict(auth_output)
        row = "claude auth      ok" if verdict is None else verdict
    else:
        verdict = env_verdict(env_names, lambda name: run(ssh_argv(host.ssh, env_check_argv(name)))[1])
        row = "env vars      ok" if verdict is None else verdict
    return (1 if verdict is not None and code == 0 else code), [*output.splitlines(), row]
