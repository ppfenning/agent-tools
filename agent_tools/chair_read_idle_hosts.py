"""The `hosts` reader for the idle-stall facts: one `HostCheck` per configured lane host.

The ssh and login check already runs and is recorded: `chair_login_check.check_login_on_host` merges
`login_ok` and `reason` into the host row's `versions_json`. This reader only reads that record through
`run_store.hosts`; it runs no probe and opens no ssh. A timed-out check surfaces as whatever `reason` the
writer stored, and as the fixed `NO_REASON` detail when it stored none.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_tools import lane_hosts, run_store
from agent_tools.chair_login_watch import _versions
from agent_tools.chair_types import HostCheck

NO_REASON = "login check failed"


def _check(name: str, row: Mapping[str, Any] | None) -> HostCheck:
    """A host with no row, or no `login_ok` in its row, is ok: absence is not a failed check."""
    versions = {} if row is None else _versions(row)
    if versions.get("login_ok") is not False:
        return {"host": name, "ok": True, "detail": ""}
    reason = versions.get("reason")
    return {"host": name, "ok": False, "detail": reason if isinstance(reason, str) and reason else NO_REASON}


def host_checks(names: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> list[HostCheck]:
    """One `HostCheck` per name, in order, from `run_store.hosts` rows holding each host's newest check."""
    by_name = {str(row.get("name") or ""): row for row in rows}
    return [_check(name, by_name.get(name)) for name in names]


def read_idle_hosts(
    runs_dir: Path, hosts: Sequence[lane_hosts.LaneHost] | lane_hosts.LaneHostError
) -> list[HostCheck]:
    """Edge. Empty when the lane hosts are misconfigured or the store cannot be read; never raises."""
    if isinstance(hosts, lane_hosts.LaneHostError):
        return []
    try:
        return host_checks([host.name for host in hosts], run_store.hosts(runs_dir))
    except Exception:  # a tick must survive any unreadable source
        return []
