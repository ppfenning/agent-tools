"""Pure planning for carrying a stranded or split phase onto one pr branch; no clock, no I/O, no git."""

from __future__ import annotations

from collections.abc import Sequence
from functools import reduce

from agent_tools.chair_types import Action, CarryTask, StrandedPhase


def _newest_per_task(rows: Sequence[CarryTask]) -> list[CarryTask]:
    """One row per task: the one with the highest run_seq, the newest run's newest commit."""
    newest = reduce(
        lambda acc, row: acc if row["task"] in acc and acc[row["task"]]["run_seq"] >= row["run_seq"] else {**acc, row["task"]: row},
        rows,
        {},
    )
    return [newest[task] for task in sorted(newest)]


def _order_picks(picks: Sequence[CarryTask]) -> tuple[list[CarryTask], list[str]]:
    """Picks ordered after their carried needs, ties by task id; the second value lists tasks stuck in a needs cycle."""
    by_task = {p["task"]: p for p in picks}

    def step(state: tuple[tuple[str, ...], frozenset[str]]) -> tuple[tuple[str, ...], frozenset[str]]:
        done, placed = state
        ready = sorted(t for t in by_task if t not in placed and all(n in placed for n in by_task[t]["needs"] if n in by_task))
        return (done, placed) if not ready else ((*done, ready[0]), placed | {ready[0]})

    def settle(state: tuple[tuple[str, ...], frozenset[str]]) -> tuple[tuple[str, ...], frozenset[str]]:
        after = step(state)
        return state if after == state else settle(after)

    order, placed = settle(((), frozenset()))
    return [by_task[t] for t in order], sorted(t for t in by_task if t not in placed)


def plan_carry(phases: Sequence[StrandedPhase]) -> list[Action]:
    """One carry_phase per phase with approved rows and nothing pending; a needs cycle yields needs_chair instead."""

    def one(phase: StrandedPhase) -> list[Action]:
        if phase["pending"] or not phase["approved"]:
            return []
        picks, stuck = _order_picks(_newest_per_task(phase["approved"]))
        if stuck:
            return [_stop(phase["initiative"], phase["phase"], "carry_cycle", f"needs cycle among tasks {', '.join(stuck)}; no carry planned")]
        return [{
            "kind": "carry_phase", "initiative": phase["initiative"], "phase": phase["phase"],
            "pr_branch": f"pr/carry-{phase['initiative']}-{phase['phase']}", "picks": picks,
        }]

    return [action for phase in phases for action in one(phase)]


def _stop(initiative: str, phase: str, cause: str, reason: str) -> Action:
    return {
        "kind": "needs_chair", "initiative": initiative, "phase": phase, "cause": cause, "epoch": 0,
        "reason": f"initiative {initiative} phase {phase}: {reason}",
    }


def conflict_stop(initiative: str, phase: str, task: str, files: Sequence[str]) -> Action:
    """The needs_chair for a cherry-pick that conflicted on task; the pr branch is left for the chair."""
    return _stop(
        initiative, phase, "carry_conflict",
        f"task {task} conflicts in {', '.join(sorted(files))}; the carry stopped and left the pr branch for the chair",
    )


def checks_failed_stop(initiative: str, phase: str, failing: Sequence[str]) -> Action:
    """The needs_chair for failed repository checks or PR checks, naming each failing check."""
    return _stop(
        initiative, phase, "carry_checks_failed",
        f"checks failed: {', '.join(failing)}; the carry stopped and left the pr branch for the chair",
    )
