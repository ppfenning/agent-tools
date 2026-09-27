"""Chair housekeeping: lake sync, trace prune, runs clean, in order, none stopping the others."""

from collections.abc import Callable

LINE_CUT = 80
REASON_CUT = 200
PRUNE_SKIPPED = "prune skipped: subcommand not available"
# `cox runs clean` takes one run_id and has no exited, age or work-item-done filter.
CLEAN_MISSING = "exited, older-than-30-days and work-item-done filters"


def sync_argv() -> list[str]:
    return ["cox", "lake", "sync"]


def prune_argv(traces_root: str) -> list[str]:
    return ["python", "-m", "harness.store_backfill_traces", "prune", traces_root, "--older-than", "7"]


def clean_argv() -> list[str] | None:
    """None while `cox runs clean` cannot express the filters; nothing destructive runs."""
    return None


def step_line(name: str, code: int, output: str) -> str:
    lines = output.strip().splitlines()
    first = lines[0][:LINE_CUT] if lines else ""
    head = f"{name}: ok" if code == 0 else f"{name}: FAILED exit {code}"
    return f"{head} {first}".rstrip()


def join_reason(lines: list[str]) -> str:
    joined = "; ".join(lines)
    if len(joined) <= REASON_CUT or not lines:
        return joined
    share = (REASON_CUT - 2 * (len(lines) - 1)) // len(lines)
    return "; ".join(line[:share] for line in lines)


def _attempt(run: Callable[[list[str]], tuple[int, str]], name: str, argv: list[str]) -> str:
    try:
        code, output = run(argv)
    except Exception as exc:  # one step's failure must not stop the others
        return step_line(name, 1, f"{type(exc).__name__}: {exc}")
    return step_line(name, code, output)


def run_housekeeping(
    run: Callable[[list[str]], tuple[int, str]],
    traces_root: str,
    prune_available: Callable[[], bool],
) -> tuple[str, str]:
    clean = clean_argv()
    lines = [
        _attempt(run, "lake sync", sync_argv()),
        _attempt(run, "prune", prune_argv(traces_root)) if prune_available() else PRUNE_SKIPPED,
        _attempt(run, "clean", clean) if clean is not None else f"clean skipped: {CLEAN_MISSING}",
    ]
    failed = any(": FAILED" in line for line in lines)
    return ("failed" if failed else "done", join_reason(lines))
