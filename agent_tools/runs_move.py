"""The plan of a move: pause the run, stop it, clear the initiative's branches, relaunch.

Pure. No I/O, no calls elsewhere; the task that executes each step is separate.
"""


def move_plan(run_id: str, initiative: str, host: str, reason: str | None = None) -> list[dict]:
    return [
        {"kind": "pause", "run": run_id, "reason": reason},
        {"kind": "stop", "run": run_id},
        {"kind": "clear_branches", "initiative": initiative},
        {"kind": "relaunch", "initiative": initiative, "host": host},
    ]
