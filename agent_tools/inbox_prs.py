"""Inbox source: open PRs the chair opened that wait on a merge. `prs_to_items` is pure; `load_pr_records` is the edge."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_tools.chair_read_review_prs import read_review_prs
from agent_tools.inbox import InboxItem, item_id

Record = Mapping[str, Any]

_FIELDS = "number,title,url,createdAt,state,isDraft,reviewDecision,mergeStateStatus,statusCheckRollup"
_PASS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_FAIL = frozenset({"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"})


def _outcomes(pr: Record) -> list[str]:
    """A check run reports `conclusion` and a status context reports `state`; neither means pending."""
    return [str(c.get("conclusion") or c.get("state") or "").upper() for c in pr.get("statusCheckRollup") or []]


def _check_status(outcomes: Sequence[str]) -> str:
    passed = sum(o in _PASS for o in outcomes)
    failed = sum(o in _FAIL for o in outcomes)
    return f"checks: {passed} passed, {failed} failed, {len(outcomes) - passed - failed} pending" if outcomes else "checks: none"


def _reason(pr: Record, outcomes: Sequence[str]) -> str:
    failed = sum(o in _FAIL for o in outcomes)
    if failed:
        return f"blocked: {failed} checks failing"
    if pr.get("mergeStateStatus") == "DIRTY":
        return "blocked: merge conflict with base"
    if any(o not in _PASS for o in outcomes):
        return "waiting on checks"
    if pr.get("reviewDecision") == "REVIEW_REQUIRED":
        return "waiting on review, then merge"
    return "ready to merge"


def _waits_on_merge(record: Record) -> bool:
    """Open, not a draft, and not sent back to its author: a draft or changes-requested PR waits on the author, not a merge."""
    pr = record.get("pr") or {}
    return "error" not in pr and pr.get("state") == "OPEN" and not pr.get("isDraft") and pr.get("reviewDecision") != "CHANGES_REQUESTED"


def _item(record: Record) -> InboxItem:
    pr, url = record["pr"], record["url"]
    outcomes = _outcomes(pr)
    return InboxItem(
        id=item_id("pr", url),
        kind="pr",
        created_at=datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00")).astimezone(UTC),
        what=f"#{pr['number']} {pr['title']}",
        evidence=f"{_check_status(outcomes)}; {_reason(pr, outcomes)}",
        accept_cmd=("gh", "pr", "merge", url, "--squash", "--delete-branch"),
        deny_cmd=("gh", "pr", "close", url),
    )


def prs_to_items(records: Sequence[Record]) -> tuple[InboxItem, ...]:
    """One `pr` item per chair record whose `pr` view waits on a merge; unreadable views are `pr_errors`, never items."""
    return tuple(_item(r) for r in records if _waits_on_merge(r))


def pr_errors(records: Sequence[Record]) -> tuple[str, ...]:
    """One line per chair PR the edge could not read, so an empty inbox is never mistaken for a failed look."""
    return tuple(f"{r['url']}: {r['pr']['error']}" for r in records if "error" in (r.get("pr") or {}))


def view_pr(url: str) -> dict:
    """Edge. `gh pr view <url> --json` as forge_github.pr_state does it; `{"error": reason}` when gh fails."""
    try:
        r = subprocess.run(["gh", "pr", "view", url, "--json", _FIELDS], capture_output=True, text=True)
    except OSError as exc:
        return {"error": str(exc)}
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip() or f"gh exited {r.returncode}"}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": f"unreadable gh output: {r.stdout.strip()}"}


def load_pr_records(runs_dir: Path, view: Callable[[str], dict] = view_pr) -> list[dict]:
    """Edge. The chair's review PRs from its task records, as dash_feed reads them, each with its `gh pr view` under `pr`."""
    rows = read_review_prs(Path(runs_dir), lambda url: {}, lambda runs_dir, runs: {})
    return [{**row, "pr": view(row["url"])} for row in rows]
