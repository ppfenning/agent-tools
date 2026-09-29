"""Plan lands for approved tasks whose phase is done: dependency order, one repository at a time."""
import heapq
from collections.abc import Collection

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


def _run_suffix(run: str) -> int:
    """The integer after a run id's last '-'; a carried phase's newest run has the highest one."""
    return int(run.rsplit("-", 1)[-1])


def newest_run(runs: Collection[str]) -> str:
    """The one run, or the highest-suffixed of several: fetching an older run of a carried phase leaves the newest absent."""
    return next(iter(runs)) if len(runs) == 1 else max(runs, key=_run_suffix)


def fetch_action(run: str, repo: str, initiative: str) -> Action:
    """The fetch a remote run's branches take before anything builds on them; plan_tick stamps the epoch."""
    return {"kind": "fetch", "run": run, "repo": repo, "initiative": initiative, "epoch": None}  # type: ignore[typeddict-item]


def plan_lands(facts: Facts) -> list[Action]:
    """A land_phase per completed phase, in `planned_tasks` order; the run it lands from is fetched first when it needs one.

    A group whose resolved run is empty gets one needs_chair (cause `no_run`) instead: a land_phase with an empty
    run has nowhere to land from, whatever moved the task to approved."""
    tasks = planned_tasks(facts["approved"], facts["initiatives"])
    groups: dict[tuple[str, str], list[ApprovedTask]] = {}
    for t in tasks:
        groups.setdefault((t["initiative"], t["phase"]), []).append(t)
    fetched: set[str] = set()
    actions: list[Action] = []  # type: ignore[assignment]  # plan_tick stamps the epoch on every action below
    for (initiative, phase), group in groups.items():
        repo = group[0]["repo"]
        run = newest_run({t["run"] for t in group})
        if not run:
            actions.append({"kind": "needs_chair", "initiative": initiative, "cause": "no_run"})
            continue
        # The fetch follows the run the land names: fetching an older run of a carried phase leaves the land's run absent.
        if run not in fetched and any(t["needs_fetch"] and t["run"] == run for t in group):
            actions.append(fetch_action(run, repo, initiative))
            fetched.add(run)
        actions.append({"kind": "land_phase", "initiative": initiative, "phase": phase, "repo": repo, "run": run, "epoch": None})
    return actions
