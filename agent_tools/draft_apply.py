"""Edge over draft_state: read a draft initiative's files, apply an approve or decline plan, mirror it to the work store."""

import getpass
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools import run_store, store_cli
from agent_tools.draft_state import Plan, Refusal, _field, _rewrite, plan_approve, plan_decline

REFUSED, STORE_STOPPED = 2, 1


@dataclass(frozen=True)
class Store:
    mode: str  # work_state.work_state_mode: "store" or "files"; "files" never touches the other two fields
    rows: Callable[[str], list[dict]]  # the work_items rows of one initiative
    set_state: Callable[..., Any]  # (initiative, task, state, by, expected) -> store_cli.SetStateResult


def store_stop(task: str, result: store_cli.SetStateResult) -> str | None:
    """None to continue, else the reason to stop, naming the task. Not-available stops: a compare-and-set that could not run proves nothing."""
    if isinstance(result, store_cli.StateSet):
        return None
    if isinstance(result, store_cli.StateRefused):
        return f"store refused {task}: it is no longer todo (now {result.current!r})" if result.current else f"store refused {task}: {result.detail}"
    if isinstance(result, store_cli.Failed):
        return f"store set-state failed for {task}: exit {result.code}: {result.detail}"
    return f"store not available; {task} was not set"


def _now() -> datetime:
    return datetime.now(UTC)


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")  # bytes, so a CRLF file keeps its line endings


def _load(work_dir: Path, initiative: str) -> tuple[Path, dict[str, Path]] | Refusal:
    """The initiative file and its tickets by task id. Tickets sit at <initiative>/<phase>/<task>.md."""
    root = work_dir / initiative
    if Path(initiative).name != initiative or not (root / "initiative.md").is_file():
        return Refusal(f"no initiative.md at {root / 'initiative.md'}")
    found = sorted(p for p in root.glob("*/*.md") if p.name != "initiative.md")
    paths = {p.stem: p for p in found}
    if len(paths) != len(found):
        return Refusal(f"two tickets in {initiative} share a task id")
    return root / "initiative.md", paths


def _mirror(verb: str, initiative: str, plan: Plan, by: str, store: Store) -> int:
    """Under `store`, move each planned task's row `--expect` its old state. A task with no row is skipped; the first stop ends it."""
    if store.mode != "store":
        return 0
    rows = store.rows(initiative)
    for task, was, to in plan.moves:
        if run_store.task_state(rows, initiative, task) is None:
            print(f"{verb}: store has no row for {task}; skipped")
            continue
        stop = store_stop(task, store.set_state(initiative, task, to, by, was))
        if stop is not None:
            print(f"{verb}: {stop}. Files already written stay written; the store and the files now disagree.")
            return STORE_STOPPED
    return 0


def _refuse(verb: str, refusal: Refusal) -> int:
    print(f"{verb}: refused: {refusal.reason}")
    return REFUSED


def _run(
    verb: str, work_dir: Path, initiative: str, by: str, store: Store, plan_of: Callable[[str, Mapping[str, str]], Plan | Refusal],
    *, dry_run: bool = False,
) -> int:
    loaded = _load(work_dir, initiative)
    if isinstance(loaded, Refusal):
        return _refuse(verb, loaded)
    initiative_path, paths = loaded
    plan = plan_of(_read(initiative_path), {task: _read(path) for task, path in paths.items()})
    if isinstance(plan, Refusal):
        return _refuse(verb, plan)
    if dry_run:
        print(f"would {verb}: {initiative}: {', '.join(f'{task} {was} -> {to}' for task, was, to in plan.moves)}")
        return 0
    for task, text in plan.tickets.items():  # tickets first: a crash leaves the draft flag set, so a rerun finishes the job
        paths[task].write_bytes(text.encode("utf-8"))
    initiative_path.write_bytes(plan.initiative_text.encode("utf-8"))
    print(f"{verb}: {initiative}: {', '.join(f'{task} {was} -> {to}' for task, was, to in plan.moves)}")
    return _mirror(verb, initiative, plan, by, store)


def approve(
    work_dir: Path, initiative: str, task_id: str | None, by: str | None, store: Store,
    *, clock: Callable[[], datetime] = _now, user: Callable[[], str] = getpass.getuser,
) -> int:
    """Edge. Exit 0 done, 2 refused with nothing written, 1 the store stopped after the files were written."""
    who, now = by or user(), clock()
    return _run("approve", work_dir, initiative, who, store, lambda text, tickets: plan_approve(text, tickets, task_id, who, now))


def decline(
    work_dir: Path, initiative: str, reason: str, by: str | None, store: Store,
    *, clock: Callable[[], datetime] = _now, user: Callable[[], str] = getpass.getuser,
) -> int:
    """Edge. Same exits as `approve`."""
    who, now = by or user(), clock()
    return _run("decline", work_dir, initiative, who, store, lambda text, tickets: plan_decline(text, tickets, reason, who, now))


def _as_declinable(initiative_text: str, tickets: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """The same files seen as a draft whose open tickets are all todo, so plan_decline accepts an approved initiative and its ready tickets."""
    return _rewrite(initiative_text, {"draft": "true"}), {
        task: _rewrite(text, {"state": "todo"}) if _field(text, "state") == "ready" else text for task, text in tickets.items()
    }


def _plan_remove(initiative_text: str, tickets: Mapping[str, str], reason: str, who: str, now: datetime) -> Plan | Refusal:
    view_initiative, view_tickets = _as_declinable(initiative_text, tickets)
    plan = plan_decline(view_initiative, view_tickets, reason, who, now)
    if isinstance(plan, Refusal):
        return plan
    # The view only renamed ready to todo; put the real old state back so the store's compare-and-set expects it.
    moves = tuple((task, _field(tickets[task], "state") or was, to) for task, was, to in plan.moves)
    return Plan(plan.initiative_text, plan.tickets, moves)


def remove(
    work_dir: Path, initiative: str, reason: str, by: str | None, store: Store,
    *, dry_run: bool = False, clock: Callable[[], datetime] = _now, user: Callable[[], str] = getpass.getuser,
) -> int:
    """Edge. Decline's path for a removal: drops ready tickets as well as todo ones, on an approved initiative too. `dry_run` writes nothing. Same exits as `approve`."""
    who, now = by or user(), clock()
    return _run(
        "remove", work_dir, initiative, who, store, lambda text, tickets: _plan_remove(text, tickets, reason, who, now), dry_run=dry_run
    )
