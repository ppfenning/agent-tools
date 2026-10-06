"""Pure remedy choice and reason trim for a quarantined task. No I/O, no clock."""

_REASON_LIMIT = 600

_REMEDY_BY_CAUSE = {
    "ticket": "re-ground",
    "code": "re-ground",
    "review": "re-ground",
    "stranded": "carry",
    "harness": "retry",
    "stalled": "retry",
    "stale": "drop",
    "superseded": "drop",
}

# cli.py has no cox verb for set-state, so these argvs use the store_cli set-state shape. `run` stands in for the
# initiative, `chair` is the --by label and `python` is the interpreter; the four inputs carry none of them.
_SET_STATE = ("python", "-m", "harness.store_cli", "set-state")


def trim_reason(text: str | None) -> str:
    """Strip the text and cut it to 600 characters; None gives an empty string."""
    return (text or "").strip()[:_REASON_LIMIT]


def choose_remedy(cause: str | None) -> str:
    """Fixed rule: any cause not listed, and a missing cause, gives re-ground."""
    return _REMEDY_BY_CAUSE.get(cause or "", "re-ground")


def _set_state(run: str, task: str, state: str) -> list[str]:
    return [*_SET_STATE, run, task, state, "--by", "chair"]


def remedy_command(kind: str, run: str, task: str, repo: str) -> list[str]:
    """Argv of the existing command for a remedy kind; an unknown kind gets the re-ground argv."""
    if kind == "carry":
        return ["cox", "runs", "land", run, "--repo", repo, "--task", task, "--apply"]
    if kind == "drop":
        # No dedicated drop verb exists: the nearest set-state argv marks the ticket dropped.
        return _set_state(run, task, "dropped")
    # Retry has no dedicated verb either: the nearest set-state argv puts the ticket ready. Re-ground and unknown kinds too.
    return _set_state(run, task, "ready")


def remedy_for(cause: str | None, run: str, task: str, repo: str) -> dict:
    kind = choose_remedy(cause)
    return {"kind": kind, "command": remedy_command(kind, run, task, repo)}
