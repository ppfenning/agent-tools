"""Approved task records dropped without a quarantine (`docs/design/triage.md` §3)."""

from __future__ import annotations

from collections.abc import Callable

from agent_tools.land import _approved as land_approved


def _scoped_item(record: dict, candidates: list[dict]) -> dict | None:
    """The one item this record's task names. A task id repeats across
    initiatives and across phases of the same initiative, so a
    `candidates` list with more than one entry resolves only when the
    record's own `initiative` and `phase` narrow it to one; otherwise this
    comes back `None` rather than guessing which item is meant."""
    initiative = record.get("initiative")
    phase = record.get("phase")
    scoped = [c for c in candidates
              if (initiative is None or c.get("initiative") == initiative)
              and (phase is None or c.get("phase") == phase)]
    return scoped[0] if len(scoped) == 1 else None


def _approved_and_unlanded(record: dict) -> bool:
    """The land's own approval rule, so a unanimously approved record (no arbiter ran) counts too."""
    return land_approved(record) is None and not record.get("landed")


def _remedy(record: dict, item: dict | None) -> str | None:
    """The exact `cox runs land` line, or `None` when no repo path can be
    resolved — a line that looks runnable and is not is worse than none."""
    repo = record.get("repo") or (item.get("repo") if item else None)
    if not repo:
        return None
    return f"cox runs land {record.get('run')} --task {record.get('task')} --repo {repo}"


def _row(record: dict, item: dict | None) -> dict:
    return {
        "run": record.get("run"), "task": record.get("task"), "phase": record.get("phase"),
        "branch": record.get("branch"), "remedy": _remedy(record, item),
    }


_LIVE_STATES = frozenset({"ready", "approved"})


def _live_pairs(records: list[dict], items: list[dict]) -> list[tuple[dict, dict | None]]:
    """Approved, unlanded records paired with their resolved item. A record
    whose task names no item is left out; one whose item is ambiguous is
    kept with `None`; one whose item is not in a live state is left out."""
    by_id: dict[str, list[dict]] = {}
    for item in items:
        by_id.setdefault(item.get("id"), []).append(item)
    pairs = []
    for record in records:
        if not _approved_and_unlanded(record):
            continue
        candidates = by_id.get(record.get("task"), [])
        if not candidates:
            continue
        item = _scoped_item(record, candidates)
        if item is not None and item.get("state") not in _LIVE_STATES:
            continue
        pairs.append((record, item))
    return pairs


def _missing(record: dict, repo_exists: Callable[[str], bool]) -> bool:
    """True when the record's own non-empty `repo` fails `repo_exists`."""
    repo = record.get("repo")
    return bool(repo) and not repo_exists(repo)


def _carried(record: dict, item: dict | None, branch_exists: Callable[[str, str], bool]) -> bool:
    """True when the record's `branch` is a bare phase branch `epic/<init>/<phase>`
    that `branch_exists` reports present in the resolved repo. `epic/...--<task>`
    is task-scoped, as in `cleanup.plan_cleanup`, so it is never carried."""
    branch = record.get("branch")
    repo = record.get("repo") or (item.get("repo") if item else None)
    return (bool(branch) and bool(repo) and branch.startswith("epic/") and "--" not in branch
            and branch_exists(repo, branch))


def stranded(records: list[dict], items: list[dict],
             repo_exists: Callable[[str], bool] | None = None,
             branch_exists: Callable[[str, str], bool] | None = None) -> list[dict]:
    """Rows for approved, unlanded records whose matching item is `ready`
    or `approved`. `items` are expected to carry a resolved `repo` (the edge
    reads it from the initiative's own frontmatter, since a work item's
    `id`/`initiative` name a ticket, not a filesystem path). A record
    whose task id does not resolve to one item under its own initiative
    and phase is still reported, with `remedy` left `None` rather than
    matched against a foreign item's state or repo. `repo_exists` is
    injected; `None` treats every repo as present, and a record with no
    `repo` is never skipped for a missing one. `branch_exists` is injected
    too; when given, a record on a bare phase branch it reports as still
    present is skipped as not stranded but waiting on its phase's carried
    branch under the one-merge-per-phase relaunch rule, while `None`, any
    task-scoped branch, and a record with no resolvable repo behave exactly
    as before."""
    return [_row(record, item) for record, item in _live_pairs(records, items)
            if (repo_exists is None or not _missing(record, repo_exists))
            and (branch_exists is None or not _carried(record, item, branch_exists))]


def missing_repos(records: list[dict], items: list[dict],
                  repo_exists: Callable[[str], bool]) -> list[str]:
    """Sorted, de-duplicated repo paths of records `stranded` skips because
    the repo is missing. A record with a non-live item is not counted."""
    return sorted({record["repo"] for record, _ in _live_pairs(records, items)
                   if _missing(record, repo_exists)})
