"""Stop a run's process, local or remote, and wait for it to exit.

`stop_kill_argv` is pure: it only builds the argv for a remote SIGTERM. `stop_run` is the
edge: it decides local vs. remote from the run's `.remote.json`, and does the actual kill,
sleep and liveness check through callables the caller injects. It never touches `os.kill`,
`subprocess` or a real ssh process itself.
"""

from __future__ import annotations

import shlex
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_tools import lane_hosts, remote_argv, remote_lane


def stop_kill_argv(ssh: str, pidfile_path: str) -> list[str]:
    return remote_argv.ssh_argv(ssh, ["sh", "-c", f"kill -TERM $(cat {shlex.quote(pidfile_path)})"])


def stop_run(
    run_id: str,
    runs_dir: Path,
    kill_local: Callable[[int, int], Any],
    run_remote: Callable[[list[str]], Any],
    is_ended: Callable[[str], bool],
    sleep: Callable[[float], Any],
    hosts: tuple[lane_hosts.LaneHost, ...] = (),
    max_seconds: float = 60,
    interval: float = 2,
) -> dict:
    record_path = remote_lane.remote_record_path(Path(runs_dir), run_id)
    if record_path.exists():
        record = remote_lane.parse_remote_record(record_path.read_text())
        if record is None:
            return {"ok": False, "run": run_id, "reason": "bad remote record"}
        host = lane_hosts.find_lane_host(hosts, record["host"])
        if host is None:
            return {"ok": False, "run": run_id, "reason": f"host {record['host']} not configured"}
        pidfile_path = f"{host.workspace_dir}/runs/{run_id}.pid"
        argv = stop_kill_argv(host.ssh, pidfile_path)
        run_remote(argv)
        where = host.name
    else:
        pidfile = Path(runs_dir) / f"{run_id}.pid"
        if not pidfile.exists():
            return {"ok": False, "run": run_id, "reason": "no pidfile"}
        pid = int(pidfile.read_text().strip())
        kill_local(pid, signal.SIGTERM)
        where = "local"

    elapsed = 0.0
    ended = is_ended(run_id)
    while not ended and elapsed < max_seconds:
        sleep(interval)
        elapsed += interval
        ended = is_ended(run_id)
    if ended:
        return {"ok": True, "run": run_id, "host": where}
    return {"ok": False, "run": run_id, "reason": "timed out waiting for exit"}
