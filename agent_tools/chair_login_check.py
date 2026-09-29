"""Edge: check a lane host's claude login, then record the result with `cox host beat`.

Reuses `remote_argv.ssh_argv`/`auth_status_argv` for the ssh invocation and `remote_doctor.auth_verdict`
for the JSON parse `cox setup doctor --host` already runs (agent_tools/remote_doctor.py), rather than a
second ssh call or a second JSON parse. Reuses `host_cmd.beat_argv` for the store_cli write, so the write
goes through the one path (`cox host beat`, an injected runner) that already knows how to reach store_cli.
This module never logs a host in. It does check the loop's own machine: the wrong belief was that every
host row is reached over ssh, but the local host runs no sshd, so ssh to it always failed and recorded a
false `login_lapsed`. A host whose name equals `socket.gethostname()` runs `claude auth status` directly.
"""
from __future__ import annotations

import socket
from collections.abc import Callable

from agent_tools import chair_login_watch, host_cmd, remote_doctor
from agent_tools.remote_argv import auth_status_argv, ssh_argv

SshRun = Callable[[list[str]], tuple[int, str]]
CliRun = Callable[[list[str]], dict]


def _login_ok(host_name: str, ssh: str, ssh_run: SshRun) -> bool | None:
    """True when `claude auth status` and the reused JSON parse confirm the host is logged in; False when
    that parse says explicitly it is not; None when the reply cannot be read at all (the command failed,
    or the reply is not the expected JSON). The loop's own host runs the plain argv through `ssh_run`;
    every other host runs it wrapped in `ssh_argv`."""
    argv = auth_status_argv() if host_name == socket.gethostname() else ssh_argv(ssh, auth_status_argv())
    _, output = ssh_run(argv)
    verdict = remote_doctor.auth_verdict(output)
    if verdict is None:
        return True
    return False if "not logged in" in verdict else None


def check_login_on_host(host_name: str, ssh: str, current_versions: dict, now: str, ssh_run: SshRun, cli_run: CliRun) -> dict:
    """Runs the login check, locally on the loop's own host and over ssh elsewhere, merges its `login_ok`
    and `now` into `current_versions` (a `None` check result merges as `login_ok: False`, since an
    unreachable host cannot be trusted to launch), writes the merge through the reused `cox host beat`
    argv, and returns the row `cli_run` prints."""
    login_ok = _login_ok(host_name, ssh, ssh_run)
    merged = chair_login_watch.merge_login_versions(current_versions, login_ok if login_ok is not None else False, now)
    return cli_run(host_cmd.beat_argv(host_name, merged))
