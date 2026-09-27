"""The `remote_unfetched` source of a chair tick: initiatives whose newest run sits on another machine,
unfetched.

`is_remote_unfetched` and `remote_unfetched_facts` are pure. `read_remote_unfetched` is the edge: it
finds each initiative's newest run through `run_store.run_ids` and reads the three facts that decide
membership through the runs directory, mirroring the run-store and remote-record pattern
`chair_read_approved.read_fetch_facts` already uses with `remote_lane.remote_record_path`.
"""
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools import run_store
from agent_tools.chair_facts import run_initiative
from agent_tools.remote_lane import remote_record_path

Booleans = tuple[bool, bool, bool]  # has_remote_record, has_local_run_dir, has_local_log


def is_remote_unfetched(has_remote_record: bool, has_local_run_dir: bool, has_local_log: bool) -> bool:
    """A run counts as remote and unfetched only with a remote record and neither local trace of it having run here."""
    return has_remote_record and not has_local_run_dir and not has_local_log


def remote_unfetched_facts(newest_runs: Mapping[str, str], booleans: Mapping[str, Booleans]) -> dict[str, str]:
    """initiative -> run id, kept only where its newest run's booleans (from `booleans`, missing is all-False) pass `is_remote_unfetched`."""
    return {
        initiative: run
        for initiative, run in newest_runs.items()
        if is_remote_unfetched(*booleans.get(run, (False, False, False)))
    }


def _run_order(run: str) -> tuple[int, str]:
    """`x-10` sorts after `x-2`: by the numeric suffix, then the name. Mirrors `chair_read_approved._run_order`."""
    tail = run.rsplit("-", 1)[-1]
    return (int(tail) if tail.isdigit() else -1, run)


def newest_run_per_initiative(runs_dir: Path, initiatives: Sequence[str]) -> dict[str, str]:
    """Edge. Each initiative's newest run id, from the store's `run_ids`; an initiative with no run is left out."""
    by_initiative: dict[str, list[str]] = {initiative: [] for initiative in initiatives}
    for run in run_store.run_ids(runs_dir):
        initiative = run_initiative(run)
        if initiative in by_initiative:
            by_initiative[initiative].append(run)
    return {
        initiative: max(runs, key=_run_order)
        for initiative, runs in by_initiative.items()
        if runs
    }


def read_remote_unfetched(runs_dir: Path, initiatives: Sequence[str]) -> dict[str, str]:
    """Edge. `gather_facts`'s `remote_unfetched` source: initiative id to run id for each initiative whose
    newest run has a `<run>.remote.json` beside `runs_dir` and no local run directory or log for that run id."""
    newest_runs = newest_run_per_initiative(runs_dir, initiatives)
    booleans = {
        run: (
            remote_record_path(runs_dir, run).exists(),
            (runs_dir / run).is_dir(),
            (runs_dir / f"{run}.log").exists(),
        )
        for run in newest_runs.values()
    }
    return remote_unfetched_facts(newest_runs, booleans)
