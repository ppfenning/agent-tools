"""The `stall_candidates` reader: local/remote, last call and USR1-sent history for every live run.

`hosts` is accepted for parity with the other lane-host-taking readers `_chair_run_deps` wires; a live
run's remote host already rides in its own `.remote.json` record, so this reader never needs the list.
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from agent_tools import lane_hosts, remote_lane, run_store
from agent_tools.chair_types import LastCall, StallCandidate


def _local(runs_dir: Path, run: str) -> bool:
    """True when the run has no `<run>.remote.json`; `runs_stop.stop_run` checks the same file the same way.

    A file present but unparseable as a remote record is treated the same as no file: a record that
    cannot be trusted is not evidence the run is remote."""
    path = remote_lane.remote_record_path(runs_dir, run)
    if not path.exists():
        return True
    return remote_lane.parse_remote_record(path.read_text()) is None


def _last_call(runs_dir: Path, run: str) -> LastCall | None:
    """The run's newest `node_calls` row: `ts` from `run_store.last_call_at`, `role` and `task` from a
    further read on the same `_open`/`_sql` seam `_usr1_sent` uses to reach `chair_actions`. None with
    no rows yet, or when the further read cannot confirm one."""
    ts = run_store.last_call_at(runs_dir, [run]).get(run)
    if ts is None:
        return None
    try:
        opened = run_store._open(runs_dir)
    except (*run_store._DB_ERRORS, RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return None
    if opened is None:
        return None
    conn, token = opened
    try:
        sql = run_store._sql(
            "SELECT role, task_id FROM node_calls WHERE run_id = {p} AND ts = {p} ORDER BY seq DESC LIMIT 1", token
        )
        row = conn.execute(sql, (run, ts)).fetchone()
    except run_store._DB_ERRORS:
        return None
    finally:
        conn.close()
    return None if row is None else {"role": row["role"], "task": row["task_id"], "ts": ts}


def _usr1_sent(runs_dir: Path, run: str) -> bool:
    """True when `chair_actions` has a `stalled_usr1` row targeting `run`.

    Mirrors `chair_read_housekeeping.read_last_housekeeping`'s exact seam: False, never raising, with
    no store, no `chair_actions` table, an unreachable Postgres, or the driver absent."""
    try:
        opened = run_store._open(runs_dir)
    except (*run_store._DB_ERRORS, RuntimeError):  # an unreachable Postgres, or psycopg not installed
        return False
    if opened is None:
        return False
    conn, token = opened
    try:
        sql = run_store._sql("SELECT 1 FROM chair_actions WHERE kind = {p} AND target = {p}", token)
        rows = conn.execute(sql, ("stalled_usr1", run)).fetchall()
    except run_store._DB_ERRORS:
        return False
    finally:
        conn.close()
    return bool(rows)


def _candidate(runs_dir: Path, lane: run_store.Lane) -> StallCandidate:
    return {
        "run": lane.run,
        "initiative": run_store._lease_name(lane.run).removeprefix("runs:"),
        "local": _local(runs_dir, lane.run),
        "started_at": lane.launched_at,
        "last_call": _last_call(runs_dir, lane.run),
        "usr1_sent": _usr1_sent(runs_dir, lane.run),
    }


def read_stall_candidates(
    runs_dir: Path, hosts: Sequence[lane_hosts.LaneHost] | lane_hosts.LaneHostError, now: datetime
) -> list[StallCandidate]:
    """Edge. One `StallCandidate` per currently live run, local or remote.

    Live runs come from `run_store.live_lanes`, the same helper `runs_detail_screen.facts_for` and
    `runs_top_screen` already call for liveness; a lane's own `launched_at` is the run's start time,
    the same column `run_store.run_started` reads, so `started_at` needs no second read of that column."""
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return [_candidate(runs_dir, lane) for lane in run_store.live_lanes(runs_dir, now_iso)]
