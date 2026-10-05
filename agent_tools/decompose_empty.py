"""Pure rules for a decompose that ends with no tasks, and the edit-gated relaunch.

Plain data in, plain data out. No file, clock or store access: the caller passes
the task count, lint lines, stub presence and mtimes.

The hold record is the shape the needs_chair row carries. The launch wiring
builds it with `hold_record` and the relaunch gate reads it back through
`relaunch_allowed`. Its keys:

- `intake`: str, the path of the intake file the decompose ran on.
- `intake_mtime`: float, that file's mtime when the hold was written.
- `reason`: str, why the decompose produced no tasks.
"""

_NO_TASKS_REASON = "decompose produced no tasks"


def empty_outcome(task_count: int, lint_lines: list[str], stub_exists: bool) -> dict:
    """`{"empty": False}` for any task count above zero, else the hold outcome with a reason."""
    if task_count > 0:
        return {"empty": False}
    return {
        "empty": True,
        "remove_stub": stub_exists,
        "reason": "\n".join(lint_lines) if lint_lines else _NO_TASKS_REASON,
    }


def hold_record(intake_path: str, intake_mtime: float, reason: str) -> dict:
    """The needs_chair hold record: keys `intake`, `intake_mtime` and `reason`."""
    return {"intake": intake_path, "intake_mtime": intake_mtime, "reason": reason}


def relaunch_allowed(intake_mtime: float, hold: dict | None) -> bool:
    """True with no hold, or when the intake was edited after the hold was written."""
    if hold is None:
        return True
    return intake_mtime > hold["intake_mtime"]
