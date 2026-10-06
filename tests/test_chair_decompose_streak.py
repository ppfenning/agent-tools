from agent_tools.chair_decompose_streak import (
    DecomposeRun,
    DecomposeStreak,
    at_limit,
    streak,
)


def _run(run_id: str, wrote: bool, refusal: str | None = None) -> DecomposeRun:
    return DecomposeRun(run_id=run_id, wrote_items=wrote, refusal_line=refusal)


def test_no_runs_gives_zero() -> None:
    assert streak(()) == DecomposeStreak(0, (), None)


def test_one_empty_run_is_not_at_limit() -> None:
    s = streak([_run("a", False)])
    assert s.empty_count == 1
    assert not at_limit(s)


def test_two_empty_runs_are_at_limit() -> None:
    s = streak([_run("a", False), _run("b", False)])
    assert s.empty_count == 2
    assert at_limit(s)


def test_empty_then_writing_gives_zero() -> None:
    assert streak([_run("a", False), _run("b", True)]).empty_count == 0


def test_writing_then_two_empty_gives_two_oldest_first() -> None:
    s = streak([_run("a", True), _run("b", False), _run("c", False)])
    assert (s.empty_count, s.run_ids) == (2, ("b", "c"))


def test_empty_writing_empty_gives_one() -> None:
    s = streak([_run("a", False), _run("b", True), _run("c", False)])
    assert (s.empty_count, s.run_ids) == (1, ("c",))


def test_first_refusal_is_from_oldest_refusing_run_in_streak() -> None:
    s = streak(
        [
            _run("a", True, "approved but not executed: stale"),
            _run("b", False),
            _run("c", False, "approved but not executed: first"),
            _run("d", False, "approved but not executed: second"),
        ]
    )
    assert s.first_refusal == "approved but not executed: first"
