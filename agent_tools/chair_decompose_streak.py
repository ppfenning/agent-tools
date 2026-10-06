"""Pure empty-decompose streak: has an intake hit EMPTY_DECOMPOSE_LIMIT. No file, process or clock access."""
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import takewhile

EMPTY_DECOMPOSE_LIMIT = 2


@dataclass(frozen=True)
class DecomposeRun:
    run_id: str
    wrote_items: bool
    refusal_line: str | None  # first "approved but not executed" log line, None when the run printed none


@dataclass(frozen=True)
class DecomposeStreak:
    empty_count: int
    run_ids: tuple[str, ...]  # oldest first
    first_refusal: str | None


def streak(runs: Sequence[DecomposeRun]) -> DecomposeStreak:
    """Trailing runs that wrote nothing; `runs` is ordered oldest first."""
    tail = tuple(reversed(tuple(takewhile(lambda r: not r.wrote_items, reversed(runs)))))
    return DecomposeStreak(
        empty_count=len(tail),
        run_ids=tuple(r.run_id for r in tail),
        first_refusal=next((r.refusal_line for r in tail if r.refusal_line is not None), None),
    )


def at_limit(s: DecomposeStreak) -> bool:
    return s.empty_count >= EMPTY_DECOMPOSE_LIMIT
