"""Edge: check a lane host's claude login, then record the result with `cox host beat`.

Reuses `remote_argv.ssh_argv`/`auth_status_argv` for the ssh invocation and `remote_doctor.auth_verdict`
for the JSON parse `cox setup doctor --host` already runs (agent_tools/remote_doctor.py), rather than a
second ssh call or a second JSON parse. Reuses `host_cmd.beat_argv` for the store_cli write, so the write
goes through the one path (`cox host beat`, an injected runner) that already knows how to reach store_cli.
This module never logs a host in. It does check the loop's own machine: the wrong belief was that every
host row is reached over ssh, but the local host runs no sshd, so ssh to it always failed and recorded a
false `login_lapsed`. A host whose name equals `socket.gethostname()` runs `claude auth status` directly.

A host running a `runner` other than `claude-code` has no `claude` CLI to ask, so its readiness check
reads `env_names` (a provider profile's `auth_env`/`endpoint_env`) off the host instead, one `printenv`
per name, over the same local/ssh split. `claude` is never invoked on that branch.
"""
from __future__ import annotations

import socket
from collections.abc import Callable

from agent_tools import chair_login_watch, host_cmd, remote_doctor
from agent_tools.remote_argv import auth_status_argv, env_check_argv, ssh_argv

SshRun = Callable[[list[str]], tuple[int, str]]
CliRun = Callable[[list[str]], dict]


def _remote_argv(host_name: str, ssh: str, argv: list[str]) -> list[str]:
    """`argv` unwrapped on the loop's own host (`host_name == socket.gethostname()`); `ssh_argv`-wrapped
    everywhere else."""
    return argv if host_name == socket.gethostname() else ssh_argv(ssh, argv)


def _login_status(host_name: str, ssh: str, ssh_run: SshRun) -> tuple[bool | None, str]:
    """`claude auth status` run once: the verdict `_login_ok` documents, and the captured output, which
    holds a Tailscale re-auth URL when check mode asked for one."""
    _, output = ssh_run(_remote_argv(host_name, ssh, auth_status_argv()))
    verdict = remote_doctor.auth_verdict(output)
    if verdict is None:
        return True, output
    return (False if "not logged in" in verdict else None), output


def _login_ok(host_name: str, ssh: str, ssh_run: SshRun) -> bool | None:
    """True when `claude auth status` and the reused JSON parse confirm the host is logged in; False when
    that parse says explicitly it is not; None when the reply cannot be read at all (the command failed,
    or the reply is not the expected JSON)."""
    return _login_status(host_name, ssh, ssh_run)[0]


def _env_check(host_name: str, ssh: str, ssh_run: SshRun, env_names: tuple[str, ...]) -> tuple[bool, str | None]:
    """True with no reason when every name in `env_names` is present; False naming the first missing one,
    `"missing <name>"`, otherwise. False with a fixed reason when `env_names` is empty: a provider profile
    naming neither `auth_env` nor `endpoint_env`. `claude` is never invoked here.

    Present means exit 0 and non-empty trimmed output. The wrong belief is that non-empty output alone
    proves the variable is set: the production `ssh_run` returns stdout and stderr joined, so an ssh
    failure (exit 255, "Connection refused" on stderr) would read as present. `printenv` exits 1 on an
    unset name, so the exit code separates set from missing and from unreachable."""
    if not env_names:
        return False, "no auth_env or endpoint_env configured"
    for name in env_names:
        code, output = ssh_run(_remote_argv(host_name, ssh, env_check_argv(name)))
        if code != 0 or not output.strip():
            return False, f"missing {name}"
    return True, None


def check_login_on_host(
    host_name: str,
    ssh: str,
    current_versions: dict,
    now: str,
    ssh_run: SshRun,
    cli_run: CliRun,
    *,
    runner: str = "claude-code",
    env_names: tuple[str, ...] = (),
) -> dict:
    """Runs the readiness check, locally on the loop's own host and over ssh elsewhere, merges its
    `login_ok`, `check` and `reason` and `now` into `current_versions`, writes the merge through the
    reused `cox host beat` argv, and returns the row `cli_run` prints. `runner == "claude-code"` (the
    default) asks `claude auth status`, its `None` result merging as `login_ok: False` since an
    unreachable host cannot be trusted to launch. Any other `runner` checks `env_names` instead; `claude`
    is never invoked on that branch. A failed `claude auth status` check also stores the first https URL
    in its captured output, a timed-out ssh's partial output included, as `login_url`; a passing check
    clears any stored one."""
    login_url = None
    if runner == "claude-code":
        status, output = _login_status(host_name, ssh, ssh_run)
        login_ok, check, reason = status is True, "claude_auth", None
        login_url = None if login_ok else chair_login_watch.login_url_from_output(output)
    else:
        login_ok, reason = _env_check(host_name, ssh, ssh_run, env_names)
        check = "env_vars"
    merged = chair_login_watch.merge_login_versions(
        current_versions, login_ok, now, check=check, reason=reason, login_url=login_url
    )
    return cli_run(host_cmd.beat_argv(host_name, merged))
