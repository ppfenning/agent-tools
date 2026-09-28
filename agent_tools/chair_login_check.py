"""Edge: check a lane host's claude login over ssh, then record the result with `cox host beat`.

Reuses `remote_argv.ssh_argv`/`auth_status_argv` for the ssh invocation and `remote_doctor.auth_verdict`
for the JSON parse `cox setup doctor --host` already runs (agent_tools/remote_doctor.py), rather than a
second ssh call or a second JSON parse. Reuses `host_cmd.beat_argv` for the store_cli write, so the write
goes through the one path (`cox host beat`, an injected runner) that already knows how to reach store_cli.
This module never logs a host in and never checks the local machine's own login.
"""
from __future__ import annotations

from collections.abc import Callable

from agent_tools import chair_login_watch, host_cmd, remote_doctor
from agent_tools.remote_argv import auth_status_argv, ssh_argv

SshRun = Callable[[list[str]], tuple[int, str]]
CliRun = Callable[[list[str]], dict]


def _login_ok(ssh: str, ssh_run: SshRun) -> bool | None:
    """True when the reused ssh invocation and JSON parse confirm the host is logged in; False when that
    parse says explicitly it is not; None when the reply cannot be read at all (ssh failed, or the reply
    is not the expected JSON) -- a host in that state has not proven anything, either way."""
    _, output = ssh_run(ssh_argv(ssh, auth_status_argv()))
    verdict = remote_doctor.auth_verdict(output)
    if verdict is None:
        return True
    return False if "not logged in" in verdict else None


def check_login_on_host(host_name: str, ssh: str, current_versions: dict, now: str, ssh_run: SshRun, cli_run: CliRun) -> dict:
    """Runs the reused ssh check, merges its `login_ok` and `now` into `current_versions` (a `None` check
    result merges as `login_ok: False`, since an unreachable host cannot be trusted to launch), writes the
    merge through the reused `cox host beat` argv, and returns the row `cli_run` prints."""
    login_ok = _login_ok(ssh, ssh_run)
    merged = chair_login_watch.merge_login_versions(current_versions, login_ok if login_ok is not None else False, now)
    return cli_run(host_cmd.beat_argv(host_name, merged))
