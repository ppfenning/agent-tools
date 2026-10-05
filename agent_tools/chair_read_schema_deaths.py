"""The `schema_deaths` source of a chair tick: initiatives whose newest two runs both exited on the
harness schema-version refusal.

`schema_deaths` is pure. `read_schema_deaths` is the edge: it takes the run listing and exit rule from
`chair_read_exits` and reads the tail of each run's log.
"""
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_tools.chair_facts import run_initiative
from agent_tools.chair_read_exits import exit_rows, row_exited
from agent_tools.run_death_cause import SCHEMA_VERSION_CAUSE, death_cause

RunTail = tuple[str, bool, str]  # run_id, exited, log_tail


def schema_deaths(runs_by_initiative: Mapping[str, Sequence[RunTail]]) -> dict[str, list[str]]:
    """Pure. Each initiative's runs are newest first. It is present only when its newest two runs both exited and both died on the schema-version cause."""
    newest = {initiative: list(runs[:2]) for initiative, runs in runs_by_initiative.items()}
    return {
        initiative: [run_id for run_id, _, _ in two]
        for initiative, two in newest.items()
        if len(two) == 2 and all(exited and death_cause(tail) == SCHEMA_VERSION_CAUSE for _, exited, tail in two)
    }


def log_tail(runs_dir: Path, run_id: str, tail_bytes: int) -> str:
    """Edge. The last `tail_bytes` of the run's log, undecodable bytes replaced; empty when the log is missing."""
    try:
        with open(runs_dir / f"{run_id}.log", "rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - tail_bytes))
            return log.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_schema_deaths(runs_dir: Path, initiatives: Sequence[str], tail_bytes: int = 4096) -> dict[str, list[str]]:
    """Edge. `gather_facts`'s `schema_deaths` source, over the store's `runs` rows ordered newest first by `launched_at`."""
    wanted = set(initiatives)
    rows = sorted(
        (row for row in exit_rows(runs_dir) if run_initiative(str(row.get("run_id") or "")) in wanted),
        key=lambda row: str(row.get("launched_at") or ""),
        reverse=True,
    )
    by_initiative: dict[str, list[RunTail]] = {}
    for row in rows:
        run_id = str(row.get("run_id") or "")
        by_initiative.setdefault(run_initiative(run_id), []).append(
            (run_id, row_exited(row), log_tail(runs_dir, run_id, tail_bytes))
        )
    return schema_deaths(by_initiative)
