"""Plan lands for approved tasks whose phase is done: dependency order, one repository at a time."""
import heapq

from agent_tools.chair_types import Action, ApprovedTask, Facts, InitiativeFacts


def _first_of_each_id(tasks: list[ApprovedTask]) -> list[ApprovedTask]:
    """A repeated id keeps its first occurrence, both its position and its fields."""
    first = {t["id"]: t for t in reversed(tasks)}
    return [first[i] for i in dict.fromkeys(t["id"] for t in tasks)]


def _schedule(tasks: list[ApprovedTask], landed: set[str]) -> list[ApprovedTask]:
    """The longest plan that is in dependency order with each repo's lands contiguous; ties go by input order.

    A task is left for a later tick when it needs an unknown id, sits on or behind a cycle, or needs a task that
    only becomes ready after its own repo's run is over. Whatever is planned lands, so the next tick moves on.
    """
    # Kahn's algorithm as a loop with mutation local to this function: recursion hits the limit on a long chain.
    ids = {t["id"] for t in tasks}
    repo_of = {t["id"]: t["repo"] for t in tasks}
    rank = {t["id"]: n for n, t in enumerate(tasks)}
    pending = {t["id"]: len(set(t["needs"]) - landed) for t in tasks}
    dependents: dict[str, list[str]] = {i: [] for i in ids}
    for t in tasks:
        for n in set(t["needs"]) - landed:
            if n in ids:
                dependents[n].append(t["id"])
    ready: dict[str, list[int]] = {}
    for i in ids:
        if pending[i] == 0:
            heapq.heappush(ready.setdefault(repo_of[i], []), rank[i])
    planned: list[ApprovedTask] = []
    closed: set[str | None] = set()
    current: str | None = None
    while True:
        if not ready.get(current):
            closed.add(current)
            open_repos = [(heap[0], repo) for repo, heap in ready.items() if heap and repo not in closed]
            if not open_repos:
                return planned
            current = min(open_repos)[1]
        task = tasks[heapq.heappop(ready[current])]
        planned.append(task)
        for d in dependents[task["id"]]:
            pending[d] -= 1
            if pending[d] == 0 and repo_of[d] not in closed:
                heapq.heappush(ready.setdefault(repo_of[d], []), rank[d])


def planned_tasks(approved: list[ApprovedTask], initiatives: list[InitiativeFacts]) -> list[ApprovedTask]:
    """The approved tasks this tick lands, in landing order; a task `_schedule` defers is not among them."""
    landed = set().union(*(i["landed"] for i in initiatives))
    return _schedule(_first_of_each_id([t for t in approved if t["phase_done"]]), landed)


def plan_lands(facts: Facts) -> list[Action]:
    """A land per planned task; a task whose run needs a fetch has one fetch of that run planned before its first land."""
    tasks = planned_tasks(facts["approved"], facts["initiatives"])
    first_of_run = {t["run"]: t["id"] for t in reversed(tasks) if t["needs_fetch"]}
    return [
        action
        for t in tasks
        for action in (
            *(
                [{"kind": "fetch", "run": t["run"], "repo": t["repo"], "initiative": t["initiative"], "epoch": None}]  # type: ignore[list-item]  # plan_tick stamps it
                if first_of_run.get(t["run"]) == t["id"]
                else []
            ),
            {"kind": "land", "task_id": t["id"], "repo": t["repo"], "run": t["run"], "initiative": t["initiative"], "epoch": None},  # type: ignore[typeddict-item]  # plan_tick stamps it
        )
    ]
