"""The pure overlap rule every launch path shares; the edge fills `running` and `steer_streaks`."""
from collections.abc import Mapping, Sequence

from agent_tools.chair_types import Action, RunningInitiative

ESCALATE_AT = 3  # consecutive prior deferrals of one ordered pair before the chair is asked
_NEW_MARKER = " (new)"


def _clean(entry: str) -> str:
    return entry.removesuffix(_NEW_MARKER)


def _ignored(path: str) -> bool:
    """A path under a `snapshots` directory, or a lock file, never counts as shared."""
    parts = path.split("/")
    base = parts[-1]
    return "snapshots" in parts or base.endswith(".lock") or base == "package-lock.json"


def _overlap(x: str, y: str) -> str | None:
    if x == y:
        return x
    if x.endswith("/") and y.startswith(x):
        return y
    if y.endswith("/") and x.startswith(y):
        return x
    return None


def shared_paths(a: Sequence[str], b: Sequence[str]) -> list[str]:
    """Equal entries, or a `/`-ending directory and anything under it; reports the longer, sorted and unique."""
    left = [p for p in map(_clean, a) if not _ignored(p)]
    right = [p for p in map(_clean, b) if not _ignored(p)]
    found = {hit for x in left for y in right if (hit := _overlap(x, y)) is not None}
    return sorted(found)


def steer_check(
    initiative: str,
    repo: str,
    surfaces: Sequence[str],
    running: Sequence[RunningInitiative],
    streaks: Mapping[str, int],
) -> Action | None:
    """The first same-repo overlap in running order: steer_clear, or needs_chair once the streak reaches 3."""
    peers = [other for other in running if other["repo"] == repo and other["id"] != initiative]
    hit = next(((other["id"], paths) for other in peers if (paths := shared_paths(surfaces, other["surfaces"]))), None)
    if hit is None:
        return None
    other, paths = hit
    if streaks.get(f"{initiative}|{other}", 0) < ESCALATE_AT:
        return {"kind": "steer_clear", "initiative": initiative, "other": other, "paths": paths}
    return {
        "kind": "needs_chair",
        "initiative": initiative,
        "cause": "steer_deferred",
        "reason": f"{initiative} shares {', '.join(paths)} with running {other}; the pair has been deferred three ticks",
    }
