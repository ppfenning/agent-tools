"""The `lost_runs` source of a chair tick: initiatives whose lane sits on a host that has gone quiet
and whose run carries no exit record in the store.

`stale_hosts` and `lost_runs_of` are pure. `read_lost_runs` is the edge: it composes `run_store.hosts`,
`run_store.live_lanes` and `chair_read_exits.exit_rows`. The exit rule is `chair_read_exits.row_exited`,
the one `run_exited` applies, keyed here by run id rather than by initiative: a lane names its run.
"""
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import run_store
from agent_tools.chair_facts import run_initiative
from agent_tools.chair_read_exits import exit_rows, row_exited

Row = Mapping[str, Any]

DEFAULT_THRESHOLD_S = 600


def _parsed(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def stale_hosts(hosts: Sequence[Row], now: str, threshold_s: int = DEFAULT_THRESHOLD_S) -> set[str]:
    """The name of every host row whose `beat_at` is more than `threshold_s` seconds before `now`, blank,
    missing, or unparsable. Never beaten counts as stale."""
    now_parsed = _parsed(now) or datetime.min.replace(tzinfo=UTC)
    stale = set()
    for row in hosts:
        beat = _parsed(row.get("beat_at")) if row.get("beat_at") else None
        if beat is None or (now_parsed - beat).total_seconds() > threshold_s:
            stale.add(str(row.get("name") or ""))
    return stale


def lost_runs_of(lanes: Sequence[run_store.Lane], stale: set[str], exited: Mapping[str, bool]) -> dict[str, str]:
    """Pure. A lane whose host is in `stale` and whose run does not read `True` in `exited` (a run missing
    from `exited` counts as not exited) contributes its initiative, mapped onto that lane's run."""
    return {run_initiative(lane.run): lane.run for lane in lanes if lane.host in stale and exited.get(lane.run) is not True}


def read_lost_runs(runs_dir: Path, now: str) -> dict[str, str]:
    """Edge. `gather_facts`'s `lost_runs` source: initiative id to run id for each lane whose host has
    gone stale (`stale_hosts`) and whose run carries no exit record in the store's `runs` table."""
    stale = stale_hosts(run_store.hosts(runs_dir), now)
    exited = {str(row.get("run_id") or ""): row_exited(row) for row in exit_rows(runs_dir)}
    return lost_runs_of(run_store.live_lanes(runs_dir, now), stale, exited)
