"""The `pid_probe` source of a chair tick: is the pid of each remote run still alive on its host.

`pid_check_argv`, `pidfile_path` and `pid_probe_of` are pure. `read_pid_probe` is the edge: it composes the store
reads with an injected `run_ssh`, which runs an argv built by `remote_argv.ssh_argv`, the chair's one ssh seam.

A remote run's pid lives at `<host workspace_dir>/runs/<run>.pid`, the layout `remote_fetch` pulls from.
A run with no entry is unknown, never dead: a stale host, a failed or timed-out probe and a missing pidfile all
leave it out.
"""
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.chair_facts import run_initiative
from agent_tools.chair_read_docket import local_runs
from agent_tools.chair_read_lost import stale_hosts
from agent_tools.chair_types import PidProbeFact
from agent_tools.host_cmd import host_rows_to_lane_hosts
from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import ssh_argv

Row = Mapping[str, Any]
Output = tuple[int, str] | None  # (exit code, stdout) of the pid check; None when ssh could not run or timed out

# Exit 3 is "cannot tell": no readable pidfile, or no positive integer in it. Only a pid that was read is
# ever reported dead, so a wrong workspace path or a vanished pidfile never reads as a dead run.
_CHECK_SCRIPT = "\n".join((
    'pid=$(cat "$1" 2>/dev/null) || exit 3',
    "case $pid in ''|*[!0-9]*|0) exit 3;; esac",
    'kill -0 "$pid" 2>/dev/null && echo alive || echo dead',
))


def pid_check_argv(pidfile: str) -> list[str]:
    """The remote command that prints `alive` or `dead` for the pid in `pidfile`, and exits 3 when it cannot tell."""
    return ["bash", "-c", _CHECK_SCRIPT, "probe", pidfile]


def pidfile_path(workspace_dir: str, run: str) -> str:
    return f"{workspace_dir.rstrip('/')}/runs/{run}.pid"


def _beat_text(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _verdict(output: Output) -> str | None:
    """`alive` or `dead` when the check exited 0 and printed exactly that; None for anything else."""
    text = output[1].strip() if output is not None and output[0] == 0 else ""
    return text if text in ("alive", "dead") else None


def pid_probe_of(
    lanes: Sequence[run_store.Lane], hosts: Sequence[Row], outputs: Mapping[str, Output], now: str
) -> dict[str, PidProbeFact]:
    """Initiative to its probe, for each lane on a host with a fresh beat whose check printed `alive` or `dead` and
    exited 0. A lane with no output, a failed or timed-out check, other text, or a stale or unknown host has no entry.

    The host beat only gates freshness. `last_beat_at` is the lane's own lease heartbeat: a fresh host's beat is
    under the 600s stale threshold by definition, so it could never meet chair_plan's ten-minute silence rule.
    Of two lanes on one initiative, the newest by `launched_at` wins, whatever the input order."""
    stale = stale_hosts(hosts, now)
    names = {str(row.get("name") or "") for row in hosts}
    verdicts = {lane.run: _verdict(outputs.get(lane.run)) for lane in lanes}
    return {
        run_initiative(lane.run): {"alive": verdicts[lane.run] == "alive", "last_beat_at": _beat_text(lane.heartbeat_at)}
        for lane in sorted(lanes, key=lambda lane: str(lane.launched_at))
        if lane.host in names and lane.host not in stale and verdicts[lane.run] is not None
    }


def read_pid_probe(
    runs_dir: Path,
    now: str,
    run_ssh: Callable[[list[str]], Output],
    profile_hosts: Sequence[LaneHost] = (),
    local: str = "",
) -> dict[str, PidProbeFact]:
    """Edge. `gather_facts`'s `pid_probe` source. Runs one `run_ssh` per remote lane on a fresh, active host with a
    known workspace_dir; a stale host is never dialled. `now` is `YYYY-MM-DDTHH:MM:SSZ` UTC, as `live_lanes` compares text."""
    rows = run_store.hosts(runs_dir)
    _, pidfiled = local_runs(runs_dir, now)
    lanes = run_store.remote_lanes(run_store.live_lanes(runs_dir, now), pidfiled)
    reachable = {h.name: h for h in host_rows_to_lane_hosts(rows, profile_hosts, local) if h.workspace_dir}
    stale = stale_hosts(rows, now)
    targets = [(lane, reachable[lane.host]) for lane in lanes if lane.host in reachable and lane.host not in stale]
    outputs = {
        lane.run: run_ssh(ssh_argv(host.ssh, pid_check_argv(pidfile_path(host.workspace_dir, lane.run))))
        for lane, host in targets
    }
    return pid_probe_of(lanes, rows, outputs, now)
