"""Land an approved run: pick the branch it lives on, cherry-pick it onto a
fresh PR branch, open the PR, wait for it to go green, merge, and clean up.

`land_plan` and `pr_body` are pure: given the task record (the json at
`runs/<run>/tasks/<phase>/<task>.json`) and, for each candidate branch, the
commit subjects the edge found ahead of the default branch, they return the
ordered steps or a single `refuse` step naming why. `branches` already
excludes merge commits — the edge gathers it with `git log --no-merges`,
because a merge commit is a fact git holds (parent count), not something a
commit subject reliably spells out. The record is expected to carry `run`,
`task`, `phase`, and `initiative` (the edge fills the first three in from the
file's own path when the record itself is silent on them) plus `proposals`,
`review`, `arbitration`, `change_facts`, and `build`; a record silent on
`initiative` still lands cleanly off the scratch branch, and only fails to
resolve a phase branch, which becomes the ordinary "no branch is exactly one
commit ahead" refuse rather than a crash. `cli.py` is the edge: it gathers
the record and the branches with `git log`, then walks the plan through
`subprocess` and `gh`.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import shlex
import time
from collections.abc import Callable, Sequence
from pathlib import PurePath
from typing import Any

__all__ = [
    "LAUNCH_ERROR",
    "approve_to_done",
    "approved_state",
    "arbitration_verdict",
    "await_checks",
    "check_poll_result",
    "checks_argv",
    "close_to_done",
    "gate_steps",
    "gate_stop",
    "is_pending",
    "issue_closes",
    "land_log_row",
    "land_plan",
    "merge_pages",
    "phase_landable",
    "phase_pr_body",
    "phase_pr_title",
    "poll_backoff_s",
    "pr_body",
    "recover_plan",
    "recover_record",
    "rest_checks_argvs",
    "set_state_stop",
    "unreadable_poll",
    "wait_decision",
]

LAUNCH_ERROR = "refuse checks: "  # a check that cannot launch, marked so the edge refuses (exit 2) not just stops (exit 1)


def _proposal(record: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((p for p in record.get("proposals", []) if p.get("kind") == kind), None)


def _verdict(record: dict[str, Any], section: str) -> str | None:
    return (record.get(section) or {}).get("verdict")


_ARBITER_SKIPPED = "arbiter: skipped (both approved)"


def arbitration_verdict(record: dict[str, Any]) -> str | None:
    """A dict gives its "verdict"; the arbiter-skip string means both approved; null or else is None."""
    arbitration = record.get("arbitration")
    if isinstance(arbitration, dict):
        return arbitration.get("verdict")
    return "approve" if arbitration == _ARBITER_SKIPPED else None


def _approved(record: dict[str, Any]) -> str | None:
    """None when the record's decision is approve, else the reason it is not.
    An arbiter only runs on disagreement, so unanimous approval leaves no
    arbitration verdict at all — the best outcome, not a missing one."""
    arbitration = arbitration_verdict(record)
    if arbitration == "approve":
        return None
    if arbitration is not None:
        return f"arbitration verdict is {arbitration!r}, not 'approve'"
    reviews = [v for v in (_verdict(record, "review"), _verdict(record, "adversary")) if v is not None]
    if reviews and all(v == "approve" for v in reviews):
        return None
    return f"no arbitration, and the reviewers were {reviews or 'silent'}"


def checks_argv(repo_facts: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """One `(name, argv)` pair per `repo_facts["checks"]` entry (`{name, cmd}`,
    already resolved by the cartridge, §4), in the given order. A caller that
    supplies no `checks` fact keeps getting today's single launch, named
    `"tests"`: its own venv first, `uv run` next when the repo pins its
    dependencies with a lockfile, a bare `pytest -q` only when neither holds."""
    checks = repo_facts.get("checks")
    if checks:
        return [(c["name"], shlex.split(c["cmd"])) for c in checks]
    if repo_facts.get("venv_python"):
        argv = [".venv/bin/python", "-m", "pytest", "-q"]
    elif repo_facts.get("uv_lock"):
        argv = ["uv", "run", "pytest", "-q"]
    else:
        argv = ["pytest", "-q"]
    return [("tests", argv)]


def phase_landable(items: list[dict[str, Any]], records: dict[str, dict[str, Any]]) -> str | None:
    """None when every item is landable, else the first reason it is not. A `done` or `dropped` item is
    satisfied without a record; an `approved` item needs one, and any record present must be approved."""
    for item in items:
        status = item.get("status")
        if status not in ("approved", "done", "dropped"):
            return f"{item.get('id')} is {status!r}, not approved, done or dropped"
        if status == "dropped":
            continue
        record = records.get(item.get("id"))
        if record is None:
            if status == "done":
                continue
            return f"{item.get('id')} has no task record"
        refusal = _approved(record)
        if refusal is not None:
            return f"{item.get('id')}: {refusal}"
    return None


def squash_missing_files(approved_files: Sequence[dict[str, Any]], squashed_files: Sequence[str]) -> str | None:
    """None when every entry in `approved_files` (`{"task": id, "files_touched":
    [...]}`, one per approved task whose record names any) has at least one of
    its files among `squashed_files`, else the first entry's refusal naming the
    task and its files, comma-joined in the order stored on the task record."""
    for entry in approved_files:
        files = entry["files_touched"]
        if not any(f in squashed_files for f in files):
            return f"{entry['task']}: none of {', '.join(files)} is in the squash"
    return None


def phase_pr_body(phase_record: dict[str, Any], task_records: list[dict[str, Any]]) -> str:
    """The phase PR body (§2): the phase's own verdict reasoning, then one
    block per landed ticket, dropped tickets listed last with their reason."""
    verdict = (phase_record.get("phase_verdict") or {}).get("reasoning", "")
    lines = [f"Phase: {phase_record.get('phase')}", verdict]
    dropped = [r for r in task_records if r.get("status") == "dropped"]
    for r in (r for r in task_records if r.get("status") != "dropped"):
        facts = r.get("change_facts") or {}
        lines += [
            "",
            f"- {r.get('task')}: {r.get('title', r.get('task'))}",
            f"  Review: {_verdict(r, 'review')}",
            f"  Adversary: {_verdict(r, 'adversary')}",
            f"  Arbitration: {arbitration_verdict(r) or 'unanimous'}",
            f"  Fix-loop attempts: {facts.get('fix_loop_attempts')}",
            f"  Files touched: {', '.join(facts.get('files_touched', []))}",
        ]
    if dropped:
        lines += ["", "Dropped:"]
        lines += [f"- {r.get('task')}: {r.get('reason', '')}" for r in dropped]
    lines += ["", pr_footer(phase_record.get("run"))]
    return "\n".join(lines)


def phase_pr_title(initiative_id: str, phase: str, initiative_title: str) -> str:
    """`epic <id>: <phase> - <title>`, `<id>` being whatever `initiative_id` already is -- a short id under
    `ids: sequence`, the old slug key under `ids: slug` -- and `<title>` the initiative's own title. The
    ` - <title>` suffix is dropped when `initiative_title` is empty, since a caller that has not resolved
    one yet (an older record, or a test literal) should still get a readable subject."""
    subject = f"epic {initiative_id}: {phase}"
    return f"{subject} - {initiative_title}" if initiative_title else subject


def _phase_approved_files(items: list[dict[str, Any]], records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One `{"task": id, "files_touched": [...]}` per `approved` item whose
    record's `build.files_touched` is non-empty, in item order; a `done` or
    `dropped` item, or one with no or empty `files_touched`, contributes
    nothing — its files already landed in an earlier squash, or there is
    nothing to check."""
    approved = [item.get("id") for item in items if item.get("status") == "approved"]
    files_by_task = [(task, (records.get(task) or {}).get("build", {}).get("files_touched")) for task in approved]
    return [{"task": task, "files_touched": files} for task, files in files_by_task if files]


def _phase_plan(phase_record: dict[str, Any], items: list[dict[str, Any]], task_records: list[dict[str, Any]],
                repo_facts: dict[str, Any] | None, default_branch: str = "main") -> list[dict[str, Any]]:
    """§1's phase step list, or a one-step `refuse` from `phase_landable`. The
    phase branch is squashed onto a fresh `default_branch` in its own PR
    branch, the way a task land builds its PR, since a coxswain repo requires
    an up-to-date branch to merge and a phase branch cut from an older main
    cannot land as it is; `checks` and everything after run off that PR
    branch, not the phase branch itself."""
    records = {r.get("task"): r for r in task_records}
    refusal = phase_landable(items, records)
    if refusal is not None:
        return [{"kind": "refuse", "reason": refusal}]
    run, phase, initiative = phase_record.get("run"), phase_record.get("phase"), phase_record.get("initiative")
    phase_branch = f"epic/{initiative}/{phase}"
    pr_branch = f"pr/{initiative}--{phase}"
    landed_tasks = [r.get("task") for r in task_records if r.get("status") != "dropped"]
    # A `note`, not a phase-level `mark_done`: cli `_land_enrich` reads `step["task"]` on every mark_done.
    if not landed_tasks and all(item.get("status") in ("done", "dropped") for item in items):
        return [{"kind": "note", "reason": f"phase {phase} done: every item is done or dropped, nothing to land"}]
    squash_step = {"kind": "squash_phase", "branch": phase_branch, "onto": pr_branch, "from": default_branch,
                   "subject": f"epic {initiative}: {phase}"}
    approved_files = _phase_approved_files(items, records)
    if approved_files:
        squash_step = {**squash_step, "approved_files": approved_files}
    return [
        {"kind": "pick_branch", "branch": phase_branch, "commit_subject": f"phase {phase}"},
        squash_step,
        {"kind": "checks", "checks": checks_argv(repo_facts or {}), "worktree_of": pr_branch},
        {"kind": "push", "branch": pr_branch},
        {"kind": "pr_create", "title": phase_pr_title(initiative, phase, phase_record.get("initiative_title", "")),
         "body": phase_pr_body(phase_record, task_records), "head": pr_branch, "base": default_branch},
        {"kind": "wait_checks", "branch": pr_branch},
        {"kind": "merge", "squash": True, "delete_branch": True, "branch": pr_branch,
         "default_branch": default_branch, "subject": f"epic {initiative}: {phase}"},
        {"kind": "clean_phase", "run": run, "phase_branch": phase_branch, "pr_branch": pr_branch, "tasks": landed_tasks},
        *[{"kind": "mark_done", "task": t} for t in landed_tasks],
    ]


def gate_steps(steps: Sequence[dict[str, Any]], level: str) -> list[dict[str, Any]]:
    """`phase`/`epic` keep a `merge` that carries a `target` (a ticket into its phase branch) and stop before one that does not; an unknown level is `ticket`, never `full`."""
    steps = list(steps)
    if level == "full" or any(s["kind"] == "refuse" for s in steps):
        return steps
    if level in ("phase", "epic"):
        idx = next((i for i, s in enumerate(steps) if s["kind"] == "merge" and "target" not in s), None)
        return steps if idx is None else steps[:idx]
    idx = next((i for i, s in enumerate(steps) if s["kind"] == "pr_create"), None)
    note = {"kind": "note", "reason": "gate: ticket — the pull request is open and waits for a person"}
    return steps if idx is None else steps[: idx + 1] + [note]


def gate_stop(planned: Sequence[dict[str, Any]], gated: Sequence[dict[str, Any]], level: str, pr: str) -> str | None:
    """None when every planned step is kept, else the one line saying where the gate stopped and which PR stays open."""
    kept = [s for s in gated if s["kind"] != "note"]
    if len(kept) == len([s for s in planned if s["kind"] != "note"]) or any(s["kind"] == "refuse" for s in gated):
        return None
    last = kept[-1]["kind"]
    return f"gate: {level} stopped after {last}; pull request {pr or '(none opened)'} left open, unmerged"


def land_plan(record: dict[str, Any], branches: dict[str, list[str]], default_branch: str,
              repo_facts: dict[str, Any] | None = None, *, items: list[dict[str, Any]] | None = None,
              task_records: list[dict[str, Any]] | None = None, tracker: str | None = None,
              issue: str | None = None) -> list[dict[str, Any]]:
    """The ordered steps to land `record`, or a one-step `refuse`. Phase mode
    (`items` given) lands the whole phase off its own branch instead of one
    task's commit. Task mode only: `tracker` other than None or `none` adds a
    closing `route_sync`, and `issue` adds `Closes #n` to the PR body; `none`
    plans neither and says so in a note."""
    if items is not None:
        return _phase_plan(record, items, task_records or [], repo_facts, default_branch)
    if _proposal(record, "draft_pr_create") is None:
        return [{"kind": "refuse", "reason": "no draft_pr_create proposal in record"}]
    refusal = _approved(record)
    if refusal is not None:
        return [{"kind": "refuse", "reason": refusal}]

    run, task, phase = record.get("run"), record.get("task"), record.get("phase")
    initiative = record.get("initiative")
    scratch_branch = f"agents/{run}/{task}"
    phase_branch = f"epic/{initiative}/{phase}" if initiative else None
    scratch_subjects = branches.get(scratch_branch, [])
    phase_subjects = branches.get(phase_branch, []) if phase_branch else []

    if len(scratch_subjects) == 1:
        chosen, subject = scratch_branch, scratch_subjects[0]
    elif len(phase_subjects) == 1:
        chosen, subject = phase_branch, phase_subjects[0]
    else:
        found = {b: len(subs) for b, subs in branches.items()}
        return [{"kind": "refuse", "reason": f"no branch is exactly one commit ahead of {default_branch}", "found": found}]

    pr_branch = f"pr/{task}"
    draft = _proposal(record, "draft_pr_create")
    mirrored = tracker != "none"
    if not mirrored:
        sync = [{"kind": "note", "reason": "route sync skipped: tracker is none"}]
    elif tracker is None:
        sync = []
    else:
        sync = [{"kind": "route_sync", "item": task}]
    # No issue yet: sync before the PR opens so its body can say `Closes #n`.
    presync = [{"kind": "route_sync", "item": task, "before": "pr_create"}] if mirrored and tracker is not None and issue is None else []
    return [
        {"kind": "pick_branch", "branch": chosen, "commit_subject": subject},
        {"kind": "cherry_pick", "branch": chosen, "commit_subject": subject, "onto": pr_branch, "from": default_branch},
        {"kind": "checks", "checks": checks_argv(repo_facts or {}), "worktree_of": pr_branch},
        {"kind": "push", "branch": pr_branch},
        *presync,
        {"kind": "pr_create", "title": draft.get("title", subject), "body": pr_body(record, issue if mirrored else None),
         "head": pr_branch, "base": default_branch},
        {"kind": "wait_checks", "branch": pr_branch},
        {"kind": "merge", "squash": True, "delete_branch": True, "branch": pr_branch,
         "default_branch": default_branch, "subject": subject},
        {"kind": "clean", "run": run, "task": task, "branch": scratch_branch},
        {"kind": "mark_done", "task": task},
        *sync,
    ]


def with_issue(steps: Sequence[dict[str, Any]], record: dict[str, Any], issue: str | int | None) -> list[dict[str, Any]]:
    """`steps` with the `pr_create` body rebuilt to carry `Closes #n` for `issue`; unchanged for no issue."""
    if not issue:
        return list(steps)
    return [{**s, "body": pr_body(record, issue)} if s["kind"] == "pr_create" else s for s in steps]


def resume_decision(expected_tree: str, local_tree: str | None, remote_tree: str | None,
                    open_prs: Sequence[int]) -> dict[str, Any]:
    """Whether an existing `pr/<task>` branch can be reused. `fresh` when
    neither side exists, `resume` when every existing side carries
    `expected_tree` and no open PR points at it, else a `refuse` naming why."""
    sides = {"local": local_tree, "remote": remote_tree}
    existing = {side: tree for side, tree in sides.items() if tree is not None}
    if not existing:
        return {"kind": "fresh"}
    differing = [f"{side} tree {tree} differs from the cherry-picked tree {expected_tree}"
                 for side, tree in existing.items() if tree != expected_tree]
    if differing:
        return {"kind": "refuse", "reason": "; ".join(differing)}
    if open_prs:
        return {"kind": "refuse", "reason": "open pull request " + ", ".join(f"#{n}" for n in open_prs) + " points at the branch"}
    return {"kind": "resume", "local": local_tree is not None, "remote": remote_tree is not None}


def resume_steps(steps: Sequence[dict[str, Any]], decision: dict[str, Any], pr_branch: str) -> list[dict[str, Any]]:
    """`steps` with the `cherry_pick` replaced by a `reuse_branch` when
    `decision` is a `resume`; every other step keeps its place. Any other
    decision returns the steps unchanged."""
    if decision["kind"] != "resume":
        return list(steps)
    reuse = {"kind": "reuse_branch", "branch": pr_branch, "local": decision["local"], "remote": decision["remote"]}
    return [reuse if s["kind"] == "cherry_pick" else s for s in steps]


def recover_record(path: str, ticket: str | None, initiative: str) -> dict[str, Any]:
    """The `{run, task, phase, initiative}` record `recover_plan` expects, read
    off the task file's path `<run>/tasks/<phase>/<task>.json`, or a one-step
    `refuse` dict. The record's `run_id` is a truncated composite and is never
    read. A path of another shape, or a `ticket` that is not the filename, refuses."""
    parts = PurePath(path).parts
    if len(parts) < 4 or parts[-3] != "tasks" or not parts[-1].endswith(".json"):
        return {"kind": "refuse", "reason": f"{path}: not a task record path, expected <run>/tasks/<phase>/<task>.json"}
    run, phase, task = parts[-4], parts[-2], parts[-1][: -len(".json")]
    if ticket != task:
        return {"kind": "refuse", "reason": f"{path}: record ticket {ticket!r} disagrees with its filename {task!r}"}
    return {"run": run, "task": task, "phase": phase, "initiative": initiative}


def recover_plan(record: dict[str, Any], branches: dict[str, list[str]]) -> list[dict[str, Any]]:
    """The one-step merge that recovers `record`'s task commit into its phase
    branch, a one-step `already_recovered`, or a one-step `refuse`. Unlike
    `land_plan`, the target is always the phase branch, never a default
    branch: this is the remedy for a merge the harness itself refused
    because the task escalated to `self_modification`, not an ordinary land.
    `branches` maps each candidate branch this function names to the commit
    subjects the edge found ahead of the phase branch — empty means the
    branch's commit is already an ancestor (already recovered); a candidate
    absent from `branches` was not found in the repo at all."""
    run, task, phase = record.get("run"), record.get("task"), record.get("phase")
    initiative = record.get("initiative")
    if not initiative or not phase:
        return [{"kind": "refuse", "reason": f"{task}: record names no initiative/phase, cannot resolve a phase branch"}]
    phase_branch = f"epic/{initiative}/{phase}"
    candidates = [f"agents/{run}/{task}", f"epic/{initiative}/{phase}--{task}"]
    for candidate in candidates:
        if candidate not in branches:
            continue
        subjects = branches[candidate]
        if not subjects:
            return [{"kind": "already_recovered", "branch": phase_branch,
                     "reason": f"{candidate} has no commits ahead of {phase_branch}"}]
        if len(subjects) == 1:
            return [{"kind": "merge", "source": candidate, "target": phase_branch, "commit_subject": subjects[0]}]
        return [{"kind": "refuse",
                 "reason": f"{candidate} is {len(subjects)} commits ahead of {phase_branch}, expected exactly one"}]
    return [{"kind": "refuse", "reason": f"no candidate branch found for {task}: tried {', '.join(candidates)}"}]


_NO_CHECKS = "no checks reported"
# The REST poll's own "still running" marker: a returncode `gh` and `git` never exit with (EX_TEMPFAIL),
# so `gh pr checks` output from the release flow cannot read as pending, and no phrase is matched.
PENDING_RC = 75
POLL_ERROR_LIMIT = 5
_FAILED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"})


def is_pending(returncode: int) -> bool:
    return returncode == PENDING_RC


def wait_decision(returncode: int, output: str, elapsed_s: float, timeout_s: float) -> str:
    """`green`, `failed`, `retry` or `timeout`. No check registered yet retries until `timeout_s`; checks registered and still running retry with no bound, as `gh pr checks --watch` waited."""
    if returncode == 0:
        return "green"
    if is_pending(returncode):
        return "retry"
    if _NO_CHECKS in output.lower():
        return "retry" if elapsed_s < timeout_s else "timeout"
    return "failed"


def rest_checks_argvs(sha: str) -> tuple[list[str], list[str]]:
    """`gh api --paginate` argvs for a commit's check runs and legacy statuses. REST, so off the GraphQL budget; `gh` fills `{owner}/{repo}` from the cwd's remote."""
    base = f"repos/{{owner}}/{{repo}}/commits/{sha}"
    return (["gh", "api", "--paginate", f"{base}/check-runs?per_page=100"],
            ["gh", "api", "--paginate", f"{base}/status?per_page=100"])


def _json_objects(text: str) -> list[dict[str, Any]] | None:
    rest = text.lstrip()
    if not rest:
        return []
    try:
        obj, end = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        return None
    tail = _json_objects(rest[end:])
    return [obj, *tail] if tail is not None and isinstance(obj, dict) else None


def merge_pages(text: str, key: str) -> dict[str, Any] | None:
    """One body from `gh api --paginate` output, which is the pages' JSON objects back to back: the first page with every page's `key` rows. `None` when unparseable or empty."""
    pages = _json_objects(text)
    if not pages:
        return None
    return {**pages[0], key: [row for page in pages for row in page.get(key) or []]}


def check_poll_result(check_runs: dict[str, Any], status: dict[str, Any]) -> tuple[int, str]:
    """The `(returncode, output)` pair `wait_decision` reads, from the two REST bodies. A failure beats pending, as `--fail-fast` did. Fewer rows than `total_count` is pending, never green."""
    runs = [(r.get("name", ""), r.get("status"), r.get("conclusion")) for r in check_runs.get("check_runs") or []]
    statuses = [(s.get("context", ""), s.get("state")) for s in status.get("statuses") or []]
    runs_total, statuses_total = check_runs.get("total_count") or 0, status.get("total_count") or 0
    combined_failed = ["combined status"] if status.get("state") in ("failure", "error") else []
    failed = [n for n, _, c in runs if c in _FAILED_CONCLUSIONS] + (
        [n for n, st in statuses if st in ("failure", "error")] or combined_failed)
    unread = ([f"{len(runs)} of {runs_total} check runs read"] if len(runs) < runs_total else []) + (
        [f"{len(statuses)} of {statuses_total} statuses read"] if len(statuses) < statuses_total else [])
    pending = [n for n, st, _ in runs if st != "completed"] + [n for n, st in statuses if st == "pending"] + unread
    if failed:
        return 1, f"failing checks: {', '.join(failed)}"
    if pending:
        return PENDING_RC, f"checks pending: {', '.join(pending)}"
    if not runs and not statuses:
        return 1, _NO_CHECKS
    return 0, ""


def poll_backoff_s(errors: int) -> float:
    """Extra seconds to wait after `errors` unreadable polls in a row: 30, 60, 120, 240, capped at 240."""
    return float(min(15 * 2**errors, 240))


def unreadable_poll(errors: int, detail: str) -> tuple[int, str]:
    """A poll whose `gh api` or `git` call failed, `errors` times in a row: pending below `POLL_ERROR_LIMIT`, so one 502 or rate-limit reply does not fail a land; failed at the limit."""
    if errors < POLL_ERROR_LIMIT:
        return PENDING_RC, f"checks unreadable, retrying ({errors}/{POLL_ERROR_LIMIT}): {detail}"
    return 1, f"checks unreadable {errors} polls in a row: {detail}"


def await_checks(poll, timeout_s: float = 180.0, sleep=time.sleep, now=time.monotonic) -> tuple[bool, str]:
    """`poll() -> (returncode, output)` until green or failed. No check yet means not yet: retry every 15s for `timeout_s`."""
    started, waiting = now(), False
    while True:
        rc, output = poll()
        decision = wait_decision(rc, output, now() - started, timeout_s)
        if decision == "retry":
            if not waiting:
                print(f"{output.strip()}; polling every 15s until they finish" if is_pending(rc)
                      else f"no checks reported yet, waiting up to {timeout_s:.0f}s for the first one to appear")
            waiting = True
            sleep(15)
        elif decision == "timeout":
            return False, f"no checks reported within {timeout_s:.0f}s"
        else:
            return decision == "green", "green" if decision == "green" else output.strip()


def issue_closes(issue: str | int | None) -> str | None:
    """`Closes #n` for a bare `n`, `#n`, or a legacy `owner/repo#n` or `owner/n`; the issue lives in the PR's own repo, so an owner is never emitted."""
    m = re.fullmatch(r"(?:[\w.-]+/[\w.-]+#|[\w.-]+/|#)?(\d+)", str(issue or "").strip())
    return f"Closes #{m[1]}" if m else None


def pr_footer(run: str | None) -> str:
    """The trailing line every PR body ends with: the installed `coxswain-tools`
    version, the distribution `cli._package_version` reads, never fetched over
    the network; "unknown" when it is not installed; `run` appended when set."""
    try:
        version = importlib.metadata.version("coxswain-tools")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    footer = f"🚣 Built with [coxswain](https://github.com/ppfenning/coxswain) {version}"
    return f"{footer} · run {run}" if run is not None else footer


def pr_body(record: dict[str, Any], issue: str | int | None = None) -> str:
    """The PR description: verdicts, fix-loop attempts, checks, and cost if present, then the `Closes` line for `issue`."""
    lines = [f"Run: {record.get('run')}", f"Task: {record.get('task')}"]
    review, arbitration = _verdict(record, "review"), arbitration_verdict(record)
    if review:
        lines.append(f"Review verdict: {review}")
    if arbitration:
        lines.append(f"Arbitration verdict: {arbitration}")
    facts = record.get("change_facts") or {}
    if facts.get("fix_loop_attempts") is not None:
        lines.append(f"Fix-loop attempts: {facts['fix_loop_attempts']}")
    if facts.get("checks"):
        lines.append(f"Checks: {facts['checks']}")
    summary = (record.get("build") or {}).get("summary")
    if summary:
        lines += ["", summary]
    cost = record.get("cost_usd", (record.get("usage") or {}).get("cost_usd"))
    try:
        cost = None if cost is None else float(cost)
    except (TypeError, ValueError):
        cost = None
    if cost is not None:
        lines.append(f"Cost: ${cost:.2f}")
    closes = issue_closes(issue)
    lines = lines + ["", closes] if closes else lines
    return "\n".join(lines + ["", pr_footer(record.get("run"))])


def land_log_row(ts: str, run: str, task: str | None, steps_reached: Sequence[str], exit_code: int, pr: str | None) -> dict[str, Any]:
    """One `land.jsonl` line: step kinds in the order they ran, and an empty `pr` is `None`."""
    return {"ts": ts, "run": run, "task": task, "steps_reached": list(steps_reached), "exit": exit_code, "pr": pr or None}


_READY_MERGED_NOTE = "land: work item was still ready (quarantined after approval, or run on another machine); merged, so done"


def approve_to_done(text: str, *, merged: bool = False) -> tuple[str | None, str | None]:
    """The work item's frontmatter `state: approved` line rewritten to
    `state: done`, with every other byte of `text` untouched, paired with
    `None`; or `None` paired with `None` when the state is already `done`
    (nothing to do); or `None` paired with a one-line message naming the
    state when it is anything else — land never moves an unapproved item.
    Only the `---`-delimited header is searched for `state:`, so a body line
    that happens to start with `state:` is never mistaken for the field.
    With `merged` True a `ready` item also moves to `done`, paired with a
    one-line note: it was quarantined after approval, or its run was on another machine, and the merge landed it."""
    if not text.startswith("---\n"):
        return None, "land: work item has no state field"
    close = text.find("\n---\n", 4)
    header = text[:close] if close != -1 else text
    for line in header.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped.startswith("state:"):
            continue
        state = stripped[len("state:"):].strip()
        if state == "done":
            return None, None
        if state == "ready" and merged:
            return text.replace(line, line.replace("ready", "done", 1), 1), _READY_MERGED_NOTE
        if state != "approved":
            return None, f"land: work item state is {state!r}, not moving to done"
        return text.replace(line, line.replace("approved", "done", 1), 1), None
    return None, "land: work item has no state field"


def approved_state(mode: str, store_state: str | None, file_state: str | None) -> str | None:
    """The state the approved check reads: the store's when `mode` is `store` and it has a row, else the file's."""
    return store_state if mode == "store" and store_state is not None else file_state


_LANDED_ELSEWHERE = "land: another machine landed this task; the store state is no longer approved"


def set_state_stop(result: Any) -> str | None:
    """None to continue, else the reason to stop. Matches the store_cli result by class name, so land imports no store_cli.
    A refused precondition means another machine landed the task; not-available stops too, since a compare-and-set that could not run proves nothing."""
    kind = type(result).__name__
    if kind == "StateSet":
        return None
    if kind == "StateRefused":
        current = getattr(result, "current", None)
        return f"{_LANDED_ELSEWHERE} (now {current!r})" if current else _LANDED_ELSEWHERE
    if kind == "Failed":
        return f"land: store set-state failed: exit {result.code}: {result.detail}"
    return "land: store not available; cannot compare-and-set the task to done"


def close_to_done(
    text: str, *, mode: str, merged: bool, set_state: Callable[[str], Any]
) -> tuple[str | None, str | None]:
    """`approve_to_done` under `mode`. `files` is `approve_to_done` unchanged and never calls `set_state`.
    `store` calls `set_state(expected)` for `done --expect approved` only when the item would move; a stop comes back as `(None, reason)`, so the file is never rewritten."""
    new_text, message = approve_to_done(text, merged=merged)
    if mode != "store" or new_text is None:
        return new_text, message
    stop = set_state_stop(set_state("approved"))
    return (None, stop) if stop is not None else (new_text, message)


_Hunk = tuple[list[str], list[str], list[str]]


def _parse_conflict_hunks(text: str) -> list[str | _Hunk] | None:
    """`text` split into a list of plain-text segments and `(ours, base, theirs)` line-list hunks, in order.
    None when a `<<<<<<<`/`|||||||`/`=======`/`>>>>>>>` marker is missing or out of order."""
    lines = text.splitlines(keepends=True)
    segments: list[str | _Hunk] = []
    plain: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("||||||| ") or line.startswith(">>>>>>> ") or line.rstrip("\n") == "=======":
            return None
        if not line.startswith("<<<<<<< "):
            plain.append(line)
            i += 1
            continue
        if plain:
            segments.append("".join(plain))
            plain = []
        ours: list[str] = []
        i += 1
        while i < n and not lines[i].startswith("||||||| "):
            if lines[i].startswith("<<<<<<< ") or lines[i].startswith(">>>>>>> ") or lines[i].rstrip("\n") == "=======":
                return None
            ours.append(lines[i])
            i += 1
        if i >= n:
            return None
        base: list[str] = []
        i += 1
        while i < n and lines[i].rstrip("\n") != "=======":
            if lines[i].startswith("<<<<<<< ") or lines[i].startswith("||||||| ") or lines[i].startswith(">>>>>>> "):
                return None
            base.append(lines[i])
            i += 1
        if i >= n:
            return None
        theirs: list[str] = []
        i += 1
        while i < n and not lines[i].startswith(">>>>>>> "):
            if lines[i].startswith("<<<<<<< ") or lines[i].startswith("||||||| ") or lines[i].rstrip("\n") == "=======":
                return None
            theirs.append(lines[i])
            i += 1
        if i >= n:
            return None
        segments.append((ours, base, theirs))
        i += 1
    if plain:
        segments.append("".join(plain))
    return segments


def resolve_add_add_conflicts(text: str) -> str | None:
    """`text`, a worktree file left by `git -c merge.conflictStyle=diff3 cherry-pick` after an add/add conflict,
    resolved to ours-then-theirs (ours alone when ours and theirs match line-for-line). None when any hunk's
    base holds a non-empty line, or the markers do not parse into well-formed hunks."""
    segments = _parse_conflict_hunks(text)
    if segments is None:
        return None
    resolved: list[str] = []
    for segment in segments:
        if isinstance(segment, str):
            resolved.append(segment)
            continue
        ours, base, theirs = segment
        if "".join(base).strip() != "":
            return None
        resolved.append("".join(ours if ours == theirs else ours + theirs))
    return "".join(resolved)
