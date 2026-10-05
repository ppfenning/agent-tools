"""Pure guards shared by `route edit` and `route remove`: what an id names, and whether an initiative may still be changed. No I/O, no clock."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from agent_tools import draft_apply

DONE = 0  # approve and decline in draft_apply return 0 on success but name no constant for it
REFUSED = draft_apply.REFUSED  # 2: refused with nothing written
EDITABLE_STATES = frozenset({"todo", "ready"})  # any other task state means work has started or ended


@dataclass(frozen=True)
class QueuedIntake:
    task_id: str  # the intake file's stem, which is the queue row's task_id


@dataclass(frozen=True)
class InitiativeFound:
    initiative: str


@dataclass(frozen=True)
class NotFound:
    id: str


Target = QueuedIntake | InitiativeFound | NotFound


@dataclass(frozen=True)
class Ok:
    pass


@dataclass(frozen=True)
class Refusal:
    kind: str  # "live_run" or "task_past_ready"
    subject: str  # the run id or the task id
    reason: str


Guard = Ok | Refusal


def _is_queued_intake(row: Mapping, id: str) -> bool:
    extra = row.get("extra") or {}
    return row.get("kind") == "intake" and row.get("state") == "queued" and id in (row.get("task_id"), extra.get("id"))


def resolve_target(store: Sequence[Mapping], id: str) -> Target:
    """`store` is queue rows (`run_store.read_queue`). An intake resolves by file stem or frontmatter `id`; an initiative by name. Done intake is not found."""
    intake = next((row for row in store if _is_queued_intake(row, id)), None)
    if intake is not None:
        return QueuedIntake(intake["task_id"])
    if any(row.get("kind", "task") == "task" and row.get("initiative") == id for row in store):
        return InitiativeFound(id)
    return NotFound(id)


def initiative_guard(tasks: Sequence[Mapping], runs: Collection[str]) -> Guard:
    """`tasks` are one initiative's rows with `task_id` and `state`; `runs` are the ids of its live runs. A live run is reported before a task."""
    for run in sorted(runs):
        return Refusal("live_run", run, f"run {run} is live on this initiative; stop it first with: cox runs stop {run}")
    for task in tasks:
        if task.get("state") not in EDITABLE_STATES:
            return Refusal("task_past_ready", task["task_id"], f"task {task['task_id']} is {task.get('state')!r}, past ready")
    return Ok()
