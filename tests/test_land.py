import datetime
import json
import os
import subprocess as sp
from pathlib import Path

import pytest
from test_run_store import phases_table, run_row, runs_table, with_record

from agent_tools import chair, cleanup, cli, forge_github, land

_STEP_ORDER = ["pick_branch", "cherry_pick", "checks", "push", "pr_create", "wait_checks", "merge", "clean", "mark_done"]
_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


@pytest.fixture(autouse=True)
def _own_tempdir(tmp_path, monkeypatch):
    """Land worktrees live under the temp dir; give each test its own so none sees another's."""
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(cli.tempfile, "tempdir", str(tmp_path / "tmp"))


def _record(**overrides):
    base = {
        "run": "epic-x-5",
        "task": "seams-task",
        "phase": "seams",
        "initiative": "x",
        "proposals": [{"kind": "draft_pr_create", "title": "Add seams"}],
        "review": {"verdict": "approve"},
        "arbitration": {"verdict": "approve"},
        "change_facts": {"fix_loop_attempts": 1, "checks": "pytest -q"},
        "build": {"summary": "Added the seams module."},
    }
    base.update(overrides)
    return base


# --- land_plan: pure ---

def test_refuse_without_approval():
    assert land.land_plan(_record(proposals=[]), {}, "main") == [{"kind": "refuse", "reason": "no draft_pr_create proposal in record"}]
    assert land.land_plan(_record(arbitration={"verdict": "revise"}), {}, "main") == [
        {"kind": "refuse", "reason": "arbitration verdict is 'revise', not 'approve'"}
    ]


def test_arbitration_verdict_of_a_null_arbitration_is_none():
    assert land.arbitration_verdict({"arbitration": None}) is None


def test_arbitration_verdict_of_a_dict_is_its_verdict():
    assert land.arbitration_verdict({"arbitration": {"verdict": "revise"}}) == "revise"


def test_arbitration_verdict_of_the_arbiter_skip_string_is_approve():
    assert land.arbitration_verdict({"arbitration": "arbiter: skipped (both approved)"}) == "approve"


def test_land_plan_over_the_arbiter_skip_string_plans_a_normal_land():
    branches = {"agents/epic-x-5/seams-task": ["Add seams module"]}
    steps = land.land_plan(_record(arbitration="arbiter: skipped (both approved)"), branches, "main")
    assert steps[0] == {"kind": "pick_branch", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module"}


def test_branch_choice_scratch_branch():
    branches = {"agents/epic-x-5/seams-task": ["Add seams module"], "epic/x/seams": ["Add seams module", "Add other thing"]}
    steps = land.land_plan(_record(), branches, "main")
    assert steps[0] == {"kind": "pick_branch", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module"}


def test_branch_choice_phase_branch():
    branches = {"agents/epic-x-5/seams-task": ["c1", "c2"], "epic/x/seams": ["Add seams module"]}
    steps = land.land_plan(_record(), branches, "main")
    assert steps[0] == {"kind": "pick_branch", "branch": "epic/x/seams", "commit_subject": "Add seams module"}


def test_branch_choice_ambiguous_refuses_naming_what_was_found():
    branches = {"agents/epic-x-5/seams-task": ["c1", "c2"], "epic/x/seams": ["c1", "c2"]}
    steps = land.land_plan(_record(), branches, "main")
    assert steps == [{
        "kind": "refuse",
        "reason": "no branch is exactly one commit ahead of main",
        "found": {"agents/epic-x-5/seams-task": 2, "epic/x/seams": 2},
    }]


def test_step_order():
    branches = {"agents/epic-x-5/seams-task": ["Add seams module"]}
    steps = land.land_plan(_record(), branches, "main")
    assert [s["kind"] for s in steps] == _STEP_ORDER


def test_a_record_silent_on_initiative_still_lands_off_the_scratch_branch():
    branches = {"agents/epic-x-5/seams-task": ["Add seams module"]}
    steps = land.land_plan(_record(initiative=None), branches, "main")
    assert steps[0]["kind"] == "pick_branch"


def test_a_record_silent_on_initiative_refuses_cleanly_when_only_a_phase_branch_is_offered():
    branches = {"epic/x/seams": ["one commit"]}
    steps = land.land_plan(_record(initiative=None), branches, "main")
    assert steps == [{"kind": "refuse", "reason": "no branch is exactly one commit ahead of main", "found": {"epic/x/seams": 1}}]


# --- gate_steps, gate_stop: pure ---

_GATED_STEPS = [
    {"kind": "pick_branch", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module"},
    {"kind": "cherry_pick", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module",
     "onto": "pr/seams-task", "from": "main"},
    {"kind": "checks", "checks": [("tests", ["pytest", "-q"])]},
    {"kind": "push", "branch": "pr/seams-task"},
    {"kind": "pr_create", "title": "Add seams", "body": "Run: epic-x-5"},
    {"kind": "wait_checks"},
    {"kind": "merge", "squash": True, "delete_branch": True},
    {"kind": "clean", "run": "epic-x-5", "task": "seams-task", "branch": "agents/epic-x-5/seams-task"},
    {"kind": "mark_done", "task": "seams-task"},
]
_TICKET_NOTE = {"kind": "note", "reason": "gate: ticket — the pull request is open and waits for a person"}


def test_gate_steps_ticket_truncates_after_pr_create_and_appends_a_note():
    assert land.gate_steps(_GATED_STEPS, "ticket") == _GATED_STEPS[:5] + [_TICKET_NOTE]


def test_gate_steps_phase_truncates_before_a_merge_with_no_target():
    assert land.gate_steps(_GATED_STEPS, "phase") == _GATED_STEPS[:6]


def test_gate_steps_epic_truncates_before_a_merge_with_no_target():
    assert land.gate_steps(_GATED_STEPS, "epic") == _GATED_STEPS[:6]


def test_gate_steps_phase_keeps_a_merge_into_its_own_phase_branch():
    steps = _GATED_STEPS[:6] + [{"kind": "merge", "source": "agents/epic-x-5/seams-task", "target": "epic/x/seams"}]
    assert land.gate_steps(steps, "phase") == steps


def test_gate_steps_full_returns_the_steps_unchanged():
    assert land.gate_steps(_GATED_STEPS, "full") == _GATED_STEPS


def test_gate_steps_leaves_a_refusal_untouched_at_every_level():
    refusal = [{"kind": "refuse", "reason": "no draft_pr_create proposal in record"}]
    assert [land.gate_steps(refusal, level) for level in ("ticket", "phase", "epic", "full")] == [refusal] * 4


def test_gate_steps_an_unrecognized_level_fails_safe_to_ticket_not_full():
    assert land.gate_steps(_GATED_STEPS, "bogus") == _GATED_STEPS[:5] + [_TICKET_NOTE]


def test_gate_stop_names_the_level_the_last_step_and_the_open_pr():
    gated = land.gate_steps(_GATED_STEPS, "phase")
    assert land.gate_stop(_GATED_STEPS, gated, "phase", "https://x/pull/7") == (
        "gate: phase stopped after wait_checks; pull request https://x/pull/7 left open, unmerged"
    )


def test_gate_stop_at_ticket_names_pr_create_not_the_note():
    gated = land.gate_steps(_GATED_STEPS, "ticket")
    assert land.gate_stop(_GATED_STEPS, gated, "ticket", "https://x/pull/7").startswith("gate: ticket stopped after pr_create;")


def test_gate_stop_is_none_when_the_plan_runs_to_completion():
    assert land.gate_stop(_GATED_STEPS, land.gate_steps(_GATED_STEPS, "full"), "full", "https://x/pull/7") is None


# --- recover_plan: pure ---

def test_recover_plan_resolves_the_scratch_branch_into_the_phase_branch():
    branches = {"agents/epic-x-5/seams-task": ["Add seams module"]}
    steps = land.recover_plan(_record(), branches)
    assert steps == [{
        "kind": "merge",
        "source": "agents/epic-x-5/seams-task",
        "target": "epic/x/seams",
        "commit_subject": "Add seams module",
    }]


def test_recover_plan_a_commit_already_on_the_phase_branch_plans_nothing():
    branches = {"agents/epic-x-5/seams-task": []}
    steps = land.recover_plan(_record(), branches)
    assert steps == [{
        "kind": "already_recovered",
        "branch": "epic/x/seams",
        "reason": "agents/epic-x-5/seams-task has no commits ahead of epic/x/seams",
    }]


def test_recover_plan_an_unresolvable_task_refuses_rather_than_a_partial_plan():
    assert land.recover_plan(_record(), {}) == [{
        "kind": "refuse",
        "reason": "no candidate branch found for seams-task: tried agents/epic-x-5/seams-task, epic/x/seams--seams-task",
    }]
    assert land.recover_plan(_record(initiative=None), {"agents/epic-x-5/seams-task": ["x"]}) == [{
        "kind": "refuse",
        "reason": "seams-task: record names no initiative/phase, cannot resolve a phase branch",
    }]


# --- recover_record: pure ---

def test_recover_record_reads_run_phase_and_task_off_the_path():
    assert land.recover_record("runs/r-2/tasks/p/t.json", "t", "i") == {"run": "r-2", "task": "t", "phase": "p", "initiative": "i"}


def test_recover_record_refuses_a_path_without_the_tasks_segment():
    step = land.recover_record("runs/r-2/p/t.json", "t", "i")
    assert step["kind"] == "refuse"
    assert "expected <run>/tasks/<phase>/<task>.json" in step["reason"]


def test_recover_record_refuses_a_ticket_that_disagrees_with_the_filename():
    step = land.recover_record("runs/r-2/tasks/p/t.json", "other", "i")
    assert step["kind"] == "refuse"
    assert "disagrees with its filename" in step["reason"]


def test_recover_record_refuses_a_record_with_no_ticket():
    assert land.recover_record("runs/r-2/tasks/p/t.json", None, "i")["kind"] == "refuse"


# --- pr_body: pure ---

def test_pr_body_contains_verdicts_and_run_id():
    body = land.pr_body(_record())
    assert "epic-x-5" in body
    assert "Review verdict: approve" in body
    assert "Arbitration verdict: approve" in body


def test_pr_body_cost_line_survives_a_string_cost():
    assert "Cost: $1.50" in land.pr_body(_record(cost_usd="1.5"))


def test_pr_body_omits_the_cost_line_rather_than_raising_on_an_unparseable_cost():
    assert "Cost:" not in land.pr_body(_record(cost_usd="n/a"))


# --- route sync hooks: pure plan, then the edge ---

_ONE_COMMIT = {"agents/epic-x-5/seams-task": ["Add seams module"]}


def _pr_body_of(steps):
    return next(s for s in steps if s["kind"] == "pr_create")["body"]


@pytest.mark.parametrize("issue", ["7", "#7", "owner/name#7", "owner/7", 7])
def test_issue_closes_is_the_bare_number_whatever_shape_it_arrived_in(issue):
    assert land.issue_closes(issue) == "Closes #7"


@pytest.mark.parametrize("issue", [None, "", "seven", "owner/name#"])
def test_issue_closes_is_none_for_no_issue_or_an_unparseable_one(issue):
    assert land.issue_closes(issue) is None


_PRESYNCED = _STEP_ORDER[:4] + ["route_sync"] + _STEP_ORDER[4:]


def test_the_plan_carries_route_sync_after_mark_done():
    steps = land.land_plan(_record(), _ONE_COMMIT, "main", tracker="github-projects", issue="7")
    assert [s["kind"] for s in steps] == _STEP_ORDER + ["route_sync"]
    assert steps[-1] == {"kind": "route_sync", "item": "seams-task"}


def test_an_item_with_no_issue_is_synced_before_the_pr_opens_and_one_with_an_issue_is_not():
    steps = land.land_plan(_record(), _ONE_COMMIT, "main", tracker="github-projects")
    assert [s["kind"] for s in steps] == _PRESYNCED + ["route_sync"]
    assert steps[4] == {"kind": "route_sync", "item": "seams-task", "before": "pr_create"}
    assert steps[-1] == {"kind": "route_sync", "item": "seams-task"}
    assert [s["kind"] for s in land.land_plan(_record(), _ONE_COMMIT, "main", tracker="none")] == _STEP_ORDER + ["note"]


def test_with_issue_rebuilds_only_the_pr_body_and_leaves_no_issue_alone():
    steps = land.land_plan(_record(), _ONE_COMMIT, "main", tracker="github-projects")
    assert "Closes" not in _pr_body_of(steps)
    closed = land.with_issue(steps, _record(), "9")
    assert _pr_body_of(closed).endswith("\n\nCloses #9")
    assert [s for s in closed if s["kind"] != "pr_create"] == [s for s in steps if s["kind"] != "pr_create"]
    assert land.with_issue(steps, _record(), None) == steps


def test_the_pr_body_closes_the_issue_and_never_emits_an_owner():
    for issue in ("7", "owner/name#7"):
        body = _pr_body_of(land.land_plan(_record(), _ONE_COMMIT, "main", tracker="github-projects", issue=issue))
        assert body.endswith("\n\nCloses #7")
        assert "owner" not in body


def test_the_pr_body_has_no_closes_line_without_an_issue():
    assert "Closes" not in _pr_body_of(land.land_plan(_record(), _ONE_COMMIT, "main", tracker="github-projects"))


def test_tracker_none_plans_neither_the_sync_nor_the_closes_line_and_says_so():
    steps = land.land_plan(_record(), _ONE_COMMIT, "main", tracker="none", issue="7")
    assert [s["kind"] for s in steps] == _STEP_ORDER + ["note"]
    assert steps[-1]["reason"] == "route sync skipped: tracker is none"
    assert "Closes" not in _pr_body_of(steps)


def test_an_unresolved_tracker_leaves_the_plan_as_it_was():
    steps = land.land_plan(_record(), _ONE_COMMIT, "main")
    assert [s["kind"] for s in steps] == _STEP_ORDER


def test_a_skip_note_is_not_a_step_the_gate_reports_as_left_unrun():
    steps = land.land_plan(_record(), _ONE_COMMIT, "main", tracker="none")
    assert land.gate_stop(steps, land.gate_steps(steps, "full"), "full", "") is None


def _sync_step(tmp_path, tracker="github-projects"):
    """A route_sync step whose own profile and runs dir resolve `tracker`."""
    runs = tmp_path / "step-runs"; runs.mkdir(exist_ok=True)
    (runs / "policy.tracker.json").write_text(json.dumps({"tracker": tracker}), encoding="utf-8")
    return {"kind": "route_sync", "item": "seams-task", "workspace": "/w",
            "profile": str(tmp_path / "step-profile.yaml"), "runs_dir": str(runs)}


@pytest.mark.parametrize("outcome", [1, RuntimeError("boom")])
def test_execute_route_sync_calls_the_sync_in_process_and_a_failure_never_fails_the_land(monkeypatch, tmp_path, outcome):
    seen = []

    def fake(ns):
        seen.append(ns)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(cli, "_route_sync", fake)
    ok, detail = cli._execute_land_step(tmp_path, _sync_step(tmp_path))
    assert ok is True and "failed after the merge" in detail
    ns = seen[0]
    assert (ns.item, ns.workspace, ns.dry_run, ns.project, ns.profile, ns.runs_dir) == (
        "seams-task", "/w", False, None, str(tmp_path / "step-profile.yaml"), str(tmp_path / "step-runs"))


def test_execute_route_sync_reports_success(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_route_sync", lambda ns: 0)
    assert cli._execute_land_step(tmp_path, _sync_step(tmp_path)) == (True, "synced seams-task")


def test_execute_route_sync_under_tracker_none_reports_a_skip_and_never_calls_the_sync(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_route_sync", lambda ns: pytest.fail("the sync ran under tracker none"))
    assert cli._execute_land_step(tmp_path, _sync_step(tmp_path, "none")) == (
        True, "sync of seams-task skipped after the merge: tracker is none")


def test_land_enrich_gives_route_sync_its_workspace_the_items_own_id_and_the_lands_profile():
    steps = cli._land_enrich([{"kind": "route_sync", "item": "seams-task"}], path="p", worktree_root="r",
                             workspace="/w", item_id="SEAM-1", profile="/p.yaml", runs_dir="/r")
    assert steps == [{"kind": "route_sync", "item": "SEAM-1", "workspace": "/w", "profile": "/p.yaml", "runs_dir": "/r"}]


def test_land_item_facts_reads_id_and_issue_from_the_frontmatter(tmp_path):
    item = tmp_path / "t.md"
    item.write_text("---\nid: SEAM-1\nissue: 243\n---\nbody\n")
    assert cli._land_item_facts(str(item)) == ("SEAM-1", "243")
    assert cli._land_item_facts(str(tmp_path / "missing.md")) == (None, None)
    assert cli._land_item_facts(None) == (None, None)


def test_resolved_tracker_reads_the_profile_and_the_policy_file_wins(tmp_path):
    profile = {"tracker": "github-projects"}
    assert cli._resolved_tracker(profile, tmp_path) == "github-projects"
    (tmp_path / "policy.tracker.json").write_text('{"tracker": "none"}')
    assert cli._resolved_tracker(profile, tmp_path) == "none"
    (tmp_path / "policy.tracker.json").write_text("{not json")
    assert cli._resolved_tracker(profile, tmp_path) == "github-projects"


# --- cli._land_branches: the edge asks git, not prose, about merges ---

def test_land_branches_asks_git_for_no_merges_rather_than_sniffing_subjects(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._land_branches(Path("/nonexistent"), _record(), "main")
    assert calls and all("--no-merges" in c for c in calls)


def _git(root, *argv):
    sp.run(["git", "-C", str(root), *argv], check=True, capture_output=True, env=_ENV)


def _stacked_repo(tmp_path, *, parent_on_main):
    """main, plus a scratch branch carrying a parent commit then its own commit.
    With `parent_on_main` the parent's patch was squash-landed onto main."""
    root = tmp_path / "stacked"; root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "f").write_text("x"); _git(root, "add", "-A"); _git(root, "commit", "-qm", "init")
    _git(root, "checkout", "-qb", "agents/epic-x-5/seams-task")
    (root / "parent").write_text("p"); _git(root, "add", "-A"); _git(root, "commit", "-qm", "parent commit")
    (root / "own").write_text("o"); _git(root, "add", "-A"); _git(root, "commit", "-qm", "own commit")
    _git(root, "checkout", "-q", "main")
    if parent_on_main:
        (root / "parent").write_text("p"); _git(root, "add", "-A"); _git(root, "commit", "-qm", "parent squashed")
    return root


def test_land_branches_drops_a_commit_whose_patch_is_already_on_main(tmp_path):
    root = _stacked_repo(tmp_path, parent_on_main=True)
    branches = cli._land_branches(root, _record(), "main")
    assert branches["agents/epic-x-5/seams-task"] == ["own commit"]
    step = land.land_plan(_record(), branches, "main")[0]
    assert (step["kind"], step["branch"]) == ("pick_branch", "agents/epic-x-5/seams-task")


def test_land_branches_keeps_a_parent_commit_that_is_not_on_main(tmp_path):
    root = _stacked_repo(tmp_path, parent_on_main=False)
    branches = cli._land_branches(root, _record(), "main")
    assert branches["agents/epic-x-5/seams-task"] == ["own commit", "parent commit"]
    steps = land.land_plan(_record(), branches, "main")
    assert steps[0]["kind"] == "refuse" and "exactly one commit" in steps[0]["reason"]


# --- cli._execute_land_step: the git-only arms, for real, no gh ---

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    sp.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    sp.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "f").write_text("x"); sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=_ENV)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=_ENV)
    sp.run(["git", "-C", str(root), "checkout", "-qb", "agents/epic-x-5/seams-task"], check=True, env=_ENV)
    (root / "f").write_text("y"); sp.run(["git", "-C", str(root), "commit", "-aqm", "Add seams module"], check=True, env=_ENV)
    sp.run(["git", "-C", str(root), "checkout", "-q", "main"], check=True, env=_ENV)
    return root


def test_execute_cherry_pick_lands_the_one_commit_on_a_fresh_branch(repo):
    ok, detail = cli._execute_land_step(repo, {
        "kind": "cherry_pick", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module",
        "onto": "pr/seams-task", "from": "main",
    })
    assert ok, detail
    assert "pr/seams-task" in cleanup.git_branches(repo)
    assert cli._git_out(repo, "show", "pr/seams-task:f") == "y"
    assert (repo / "f").read_text() == "x"
    assert cli._git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_execute_cherry_pick_resolves_by_commit_range_not_by_subject_text(repo):
    # main already has an old commit with the same subject as the scratch
    # branch's one real commit; a --grep lookup could match the wrong one.
    sp.run(["git", "-C", repo, "commit", "--allow-empty", "-qm", "Add seams module"], check=True, env=_ENV)
    ok, detail = cli._execute_land_step(repo, {
        "kind": "cherry_pick", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module",
        "onto": "pr/seams-task", "from": "main",
    })
    assert ok, detail
    assert cli._git_out(repo, "show", "pr/seams-task:f") == "y"


def test_execute_cherry_pick_lands_a_child_whose_stacked_parent_already_merged_as_a_squash(repo):
    # The child branch carries its parent's commit and its own; main has the parent's patch under another message.
    sp.run(["git", "-C", repo, "checkout", "-qb", "agents/epic-x-6/child", "agents/epic-x-5/seams-task"], check=True, env=_ENV)
    (repo / "g").write_text("child"); sp.run(["git", "-C", repo, "add", "-A"], check=True, env=_ENV)
    sp.run(["git", "-C", repo, "commit", "-qm", "Add child"], check=True, env=_ENV)
    sp.run(["git", "-C", repo, "checkout", "-q", "main"], check=True, env=_ENV)
    (repo / "f").write_text("y"); sp.run(["git", "-C", repo, "commit", "-aqm", "Add seams module (#90)"], check=True, env=_ENV)
    ok, detail = cli._execute_land_step(repo, {
        "kind": "cherry_pick", "branch": "agents/epic-x-6/child", "commit_subject": "Add child",
        "onto": "pr/child", "from": "main",
    })
    assert ok, detail
    assert cli._git_out(repo, "show", "pr/child:g") == "child"


def test_execute_cherry_pick_replaces_a_stale_land_worktree_directory(repo):
    stale = cli._land_worktree(repo, "pr/seams-task")
    stale.mkdir(); (stale / "junk").write_text("left by a crashed land")
    ok, detail = cli._execute_land_step(repo, {
        "kind": "cherry_pick", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module",
        "onto": "pr/seams-task", "from": "main",
    })
    assert ok, detail
    assert not (stale / "junk").exists()


def test_execute_cherry_pick_conflict_reports_failure_without_raising_and_leaves_the_repo_clean(repo):
    sp.run(["git", "-C", repo, "commit", "--allow-empty", "-qm", "noop"], check=True, env=_ENV)
    (repo / "f").write_text("conflicting"); sp.run(["git", "-C", repo, "commit", "-aqm", "unrelated main change"], check=True, env=_ENV)
    ok, detail = cli._execute_land_step(repo, {
        "kind": "cherry_pick", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module",
        "onto": "pr/seams-task", "from": "main",
    })
    assert not ok
    assert detail
    status = sp.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert status.strip() == ""


def test_execute_checks_reports_pass_and_fail(tmp_path):
    assert cli._execute_land_step(tmp_path, {"kind": "checks", "checks": [("tests", ["true"])]}) == (True, "1 checks passed")
    ok, _ = cli._execute_land_step(tmp_path, {"kind": "checks", "checks": [("tests", ["false"])]})
    assert not ok


def test_execute_checks_with_a_branch_runs_in_a_worktree_and_removes_it(repo, tmp_path, monkeypatch):
    wt = tmp_path / "checks-wt"
    monkeypatch.setattr(cli.tempfile, "mkdtemp", lambda prefix="": str(wt))
    ok, detail = cli._execute_land_step(repo, {"kind": "checks", "checks": [("tests", ["true"])], "branch": "agents/epic-x-5/seams-task"})
    assert ok, detail
    assert not wt.exists()
    current = sp.run(["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
    assert current == "main"


def test_execute_checks_runs_every_check_in_order_on_a_passing_fixture(tmp_path):
    ok, detail = cli._execute_land_step(tmp_path, {
        "kind": "checks", "checks": [("lint", ["true"]), ("tests", ["true"])],
    })
    assert ok
    assert detail == "2 checks passed"


def test_execute_checks_stops_at_the_first_failing_check_and_names_it_not_its_command(tmp_path, monkeypatch):
    calls = []
    real_run = cli.subprocess.run

    def fake_run(argv, **kw):
        calls.append(argv)
        return real_run(argv, **kw)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    ok, detail = cli._execute_land_step(tmp_path, {
        "kind": "checks", "checks": [("lint", ["false"]), ("tests", ["true"])],
    })
    assert not ok
    assert detail.startswith("lint:")
    assert calls == [["false"]]


# --- land.phase_landable: pure, no I/O (§7) ---

def _item(id_, status):
    return {"id": id_, "status": status}


def test_phase_landable_all_done():
    items = [_item("a", "done"), _item("b", "done")]
    records = {"a": _record(task="a"), "b": _record(task="b")}
    assert land.phase_landable(items, records) is None


def test_phase_landable_one_item_not_ready():
    items = [_item("a", "in_progress"), _item("b", "done")]
    records = {"b": _record(task="b")}
    assert land.phase_landable(items, records) == "a is 'in_progress', not done or dropped"


def test_phase_landable_one_dropped_rest_done():
    items = [_item("a", "dropped"), _item("b", "done")]
    records = {"b": _record(task="b")}
    assert land.phase_landable(items, records) is None


def test_phase_landable_one_done_but_unapproved():
    items = [_item("a", "done"), _item("b", "done")]
    records = {"a": _record(task="a", arbitration={"verdict": "revise"}), "b": _record(task="b")}
    assert land.phase_landable(items, records) == "a: arbitration verdict is 'revise', not 'approve'"


def test_phase_landable_a_done_item_with_no_filed_task_record():
    items = [_item("a", "done"), _item("b", "done")]
    records = {"b": _record(task="b")}
    assert land.phase_landable(items, records) == "a has no task record"


# --- land_plan: phase mode (§1, §7) ---

def test_phase_plan_step_list_and_clean_phase_scoping():
    phase_record = {"run": "epic-x-5", "phase": "seams", "initiative": "x", "phase_verdict": {"reasoning": "solid"}}
    task_records = [{
        "task": "seams-task", "run": "epic-x-5", "phase": "seams", "status": "done",
        "review": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
        "change_facts": {"fix_loop_attempts": 1, "files_touched": ["a.py"]},
    }]
    items = [{"id": "seams-task", "status": "done"}]
    steps = land.land_plan(phase_record, {}, "main", items=items, task_records=task_records)
    assert [s["kind"] for s in steps] == ["pick_branch", "checks", "push", "pr_create", "wait_checks", "merge", "clean_phase", "mark_done"]
    assert next(s for s in steps if s["kind"] == "checks")["branch"] == "epic/x/seams"
    assert next(s for s in steps if s["kind"] == "clean_phase") == {
        "kind": "clean_phase", "run": "epic-x-5", "phase_branch": "epic/x/seams", "tasks": ["seams-task"],
    }


def test_phase_plan_refuses_on_an_unlandable_item():
    phase_record = {"run": "epic-x-5", "phase": "seams", "initiative": "x"}
    items = [{"id": "seams-task", "status": "in_progress"}]
    steps = land.land_plan(phase_record, {}, "main", items=items, task_records=[])
    assert steps == [{"kind": "refuse", "reason": "seams-task is 'in_progress', not done or dropped"}]


# --- land_plan: --task mode's clean step is narrowed to its own branch (§7) ---

def test_task_mode_clean_step_names_only_this_tasks_branch():
    branches = {"agents/epic-x-5/seams-task": ["Add seams module"]}
    steps = land.land_plan(_record(), branches, "main")
    assert next(s for s in steps if s["kind"] == "clean") == {
        "kind": "clean", "run": "epic-x-5", "task": "seams-task", "branch": "agents/epic-x-5/seams-task",
    }


# --- land.phase_pr_body: pure, no I/O (§2, §7) ---

def test_phase_pr_body_two_tickets_one_arbitrated_one_unanimous():
    phase_record = {"phase": "seams", "phase_verdict": {"reasoning": "Both tickets landed cleanly."}}
    task_records = [
        {"task": "seams-a", "title": "Add seams", "status": "done",
         "review": {"verdict": "approve"}, "adversary": {"verdict": "approve"}, "arbitration": {"verdict": "approve"},
         "change_facts": {"fix_loop_attempts": 0, "files_touched": ["a.py"]}},
        {"task": "seams-b", "title": "Wire seams", "status": "done",
         "review": {"verdict": "approve"}, "adversary": {"verdict": "approve"},
         "change_facts": {"fix_loop_attempts": 1, "files_touched": ["b.py", "c.py"]}},
    ]
    body = land.phase_pr_body(phase_record, task_records)
    assert body == "\n".join([
        "Phase: seams",
        "Both tickets landed cleanly.",
        "",
        "- seams-a: Add seams",
        "  Review: approve",
        "  Adversary: approve",
        "  Arbitration: approve",
        "  Fix-loop attempts: 0",
        "  Files touched: a.py",
        "",
        "- seams-b: Wire seams",
        "  Review: approve",
        "  Adversary: approve",
        "  Arbitration: unanimous",
        "  Fix-loop attempts: 1",
        "  Files touched: b.py, c.py",
    ])


# --- cli._execute_land_step: the narrowed clean and the new clean_phase (§7) ---

def test_execute_clean_deletes_only_the_named_branch_not_a_sibling(repo):
    sp.run(["git", "-C", str(repo), "branch", "agents/epic-x-5/other-task", "main"], check=True, env=_ENV)
    ok, detail = cli._execute_land_step(repo, {
        "kind": "clean", "run": "epic-x-5", "task": "seams-task",
        "branch": "agents/epic-x-5/seams-task", "worktree_root": str(repo.parent / "wt"),
    })
    assert ok, detail
    branches = cleanup.git_branches(repo)
    assert "agents/epic-x-5/seams-task" not in branches
    assert "agents/epic-x-5/other-task" in branches


def test_execute_clean_phase_deletes_only_this_phases_branches(repo):
    sp.run(["git", "-C", str(repo), "branch", "epic/x/seams", "main"], check=True, env=_ENV)
    sp.run(["git", "-C", str(repo), "branch", "epic/x/other-phase", "main"], check=True, env=_ENV)
    ok, detail = cli._execute_land_step(repo, {
        "kind": "clean_phase", "run": "epic-x-5", "phase_branch": "epic/x/seams",
        "tasks": ["seams-task"], "worktree_root": str(repo.parent / "wt"),
    })
    assert ok, detail
    branches = cleanup.git_branches(repo)
    assert "epic/x/seams" not in branches
    assert "agents/epic-x-5/seams-task" not in branches
    assert "epic/x/other-phase" in branches


# --- land.checks_argv: pure, one (name, argv) pair per resolved check (§4, §7) ---

def test_checks_argv_returns_one_pair_per_configured_check_in_order():
    facts = {"checks": [{"name": "lint", "cmd": "ruff check ."}, {"name": "tests", "cmd": "pytest -q"}]}
    assert land.checks_argv(facts) == [("lint", ["ruff", "check", "."]), ("tests", ["pytest", "-q"])]


def test_checks_argv_with_no_checks_key_returns_todays_single_pair_named_tests():
    assert land.checks_argv({}) == [("tests", ["pytest", "-q"])]
    assert land.checks_argv({"checks": []}) == [("tests", ["pytest", "-q"])]


def test_checks_argv_with_no_checks_key_still_prefers_the_repos_own_venv():
    assert land.checks_argv({"venv_python": True, "uv_lock": True}) == [("tests", [".venv/bin/python", "-m", "pytest", "-q"])]
    assert land.checks_argv({"venv_python": False, "uv_lock": True}) == [("tests", ["uv", "run", "pytest", "-q"])]


def test_land_plan_checks_step_carries_the_configured_checks():
    branches = {"agents/epic-x-5/seams-task": ["Add seams module"]}
    repo_facts = {"checks": [{"name": "lint", "cmd": "ruff check ."}, {"name": "tests", "cmd": "pytest -q"}]}
    steps = land.land_plan(_record(), branches, "main", repo_facts)
    checks = next(s for s in steps if s["kind"] == "checks")
    assert checks == {"kind": "checks", "checks": [("lint", ["ruff", "check", "."]), ("tests", ["pytest", "-q"])],
                      "worktree_of": "pr/seams-task"}


def test_execute_push_reaches_a_real_remote(repo, tmp_path):
    origin = tmp_path / "origin.git"
    sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    sp.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    ok, detail = cli._execute_land_step(repo, {"kind": "push", "branch": "main"})
    assert ok, detail
    show = sp.run(["git", "-C", str(origin), "show-ref", "refs/heads/main"], capture_output=True, text=True)
    assert show.returncode == 0


def test_execute_clean_uses_the_given_worktree_root_not_a_hardcoded_one(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "f").write_text("x"); sp.run(["git", "-C", str(root), "add", "-A"], check=True, env=_ENV)
    sp.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=_ENV)
    sp.run(["git", "-C", str(root), "branch", "epic/x/seams"], check=True)
    sp.run(["git", "-C", str(root), "branch", "agents/epic-x-5/seams-task"], check=True)
    wt = tmp_path / "wt/epic-x-5/seams-task"; wt.parent.mkdir(parents=True)
    sp.run(["git", "-C", str(root), "worktree", "add", "-q", str(wt), "agents/epic-x-5/seams-task"], check=True)
    ok, detail = cli._execute_land_step(root, {
        "kind": "clean", "run": "epic-x-5", "task": "seams-task",
        "branch": "agents/epic-x-5/seams-task", "worktree_root": str(tmp_path / "wt"),
    })
    assert ok, detail
    assert "agents/epic-x-5/seams-task" not in cleanup.git_branches(root)
    assert "epic/x/seams" in cleanup.git_branches(root)
    assert not wt.exists()


def test_execute_mark_done_writes_landed_true_to_the_record(tmp_path):
    path = tmp_path / "task.json"
    path.write_text(json.dumps(_record()), encoding="utf-8")
    ok, detail = cli._execute_land_step(tmp_path, {"kind": "mark_done", "task": "seams-task", "path": str(path)})
    assert ok, detail
    assert json.loads(path.read_text())["landed"] is True


def _mark_done_step(tmp_path, item_text):
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(_record()), encoding="utf-8")
    item_path = tmp_path / "seams-task.md"
    item_path.write_text(item_text, encoding="utf-8")
    return item_path, {"kind": "mark_done", "task": "seams-task", "path": str(task_path), "item": str(item_path)}


def test_execute_mark_done_moves_an_approved_item_to_done(tmp_path):
    item_path, step = _mark_done_step(tmp_path, "---\nid: seams-task\nstate: approved\n---\n\nBody text.\n")
    ok, detail = cli._execute_land_step(tmp_path, step)
    assert ok, detail
    assert item_path.read_text() == "---\nid: seams-task\nstate: done\n---\n\nBody text.\n"


def test_execute_mark_done_leaves_a_done_item_untouched(tmp_path):
    text = "---\nid: seams-task\nstate: done\n---\n\nBody text.\n"
    item_path, step = _mark_done_step(tmp_path, text)
    ok, detail = cli._execute_land_step(tmp_path, step)
    assert ok, detail
    assert item_path.read_text() == text


def test_execute_mark_done_leaves_a_non_approved_item_untouched_and_prints_it(tmp_path, capsys):
    text = "---\nid: seams-task\nstate: blocked\n---\n\nBody text.\n"
    item_path, step = _mark_done_step(tmp_path, text)
    ok, detail = cli._execute_land_step(tmp_path, step)
    assert ok, detail
    assert item_path.read_text() == text
    assert "blocked" in capsys.readouterr().out


def test_execute_mark_done_moves_a_ready_item_to_done_and_prints_the_note(tmp_path, capsys):
    item_path, step = _mark_done_step(tmp_path, "---\nid: seams-task\nstate: ready\n---\n\nBody text.\n")
    ok, detail = cli._execute_land_step(tmp_path, step)
    assert ok, detail
    assert item_path.read_text() == "---\nid: seams-task\nstate: done\n---\n\nBody text.\n"
    assert "merged, so done" in capsys.readouterr().out


_READY = "---\nid: t\nstate: ready\n---\n\nBody.\n"


def test_approve_to_done_moves_ready_to_done_with_a_note_when_merged():
    assert land.approve_to_done(_READY, merged=True) == (
        "---\nid: t\nstate: done\n---\n\nBody.\n",
        "land: work item was still ready (quarantined after approval, or run on another machine); merged, so done",
    )


def test_approve_to_done_refuses_ready_when_not_merged():
    assert land.approve_to_done(_READY) == (None, "land: work item state is 'ready', not moving to done")


def test_approve_to_done_refuses_blocked_even_when_merged():
    text = "---\nid: t\nstate: blocked\n---\n\nBody.\n"
    assert land.approve_to_done(text, merged=True) == (None, "land: work item state is 'blocked', not moving to done")


def test_approve_to_done_moves_approved_to_done_without_a_note():
    text = "---\nid: t\nstate: approved\n---\n\nBody.\n"
    done = "---\nid: t\nstate: done\n---\n\nBody.\n"
    assert land.approve_to_done(text) == (done, None)
    assert land.approve_to_done(text, merged=True) == (done, None)


# --- cli end to end: dry-run default, and the dirty-checkout refusal ---

def _full_gate(tmp_path):
    (tmp_path / "runs/policy.gate.json").write_text(json.dumps({"level": "full"}), encoding="utf-8")


def test_cli_dry_run_is_the_default_and_prints_the_plan(repo, tmp_path, capsys, monkeypatch):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    _full_gate(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"kind": "pick_branch"' in out
    assert "agents/epic-x-5/seams-task" in out
    assert all(kind in out for kind in _STEP_ORDER)


def test_cli_dry_run_plan_carries_the_item_path(repo, tmp_path, capsys, monkeypatch):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    item_dir = tmp_path / "work/x/seams"; item_dir.mkdir(parents=True)
    item_path = item_dir / "seams-task.md"
    item_path.write_text("---\nid: seams-task\nstate: approved\n---\n\nBody.\n", encoding="utf-8")
    _full_gate(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")])
    steps = json.loads(capsys.readouterr().out)
    assert rc == 0
    mark_done = next(s for s in steps if s["kind"] == "mark_done")
    assert mark_done["item"] == str(item_path)
    assert mark_done["from"] == "approved"
    assert mark_done["to"] == "done"


# --- cli end to end: phase land resolves the initiative from the work store ---

def _phase_land_dry_run(repo, tmp_path, capsys, monkeypatch, work_dirs):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs/epic-x-5:seams.json").write_text(json.dumps({"phase_verdict": {"reasoning": "solid"}}), encoding="utf-8")
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record(status="done")), encoding="utf-8")
    for d in work_dirs:
        (tmp_path / "work" / d).mkdir(parents=True)
        (tmp_path / "work" / d / "seams-task.md").write_text("---\nid: seams-task\nstate: done\n---\n\nBody.\n", encoding="utf-8")
    _full_gate(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["runs", "land", "epic-x-5", "--phase", "seams", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")])
    return rc, capsys.readouterr().out


def test_phase_land_finds_the_initiative_in_the_work_store_when_the_record_names_none(repo, tmp_path, capsys, monkeypatch):
    rc, out = _phase_land_dry_run(repo, tmp_path, capsys, monkeypatch, ("x/seams",))
    assert rc == 0
    assert next(s for s in json.loads(out) if s["kind"] == "pick_branch")["branch"] == "epic/x/seams"


def test_phase_land_refuses_when_two_initiatives_hold_the_phase(repo, tmp_path, capsys, monkeypatch):
    rc, out = _phase_land_dry_run(repo, tmp_path, capsys, monkeypatch, ("x/seams", "y/seams"))
    assert rc == 2
    assert "no single initiative holds phase seams" in out


# --- cli end to end: recover closes the work item too ---

def _on_disk_record(**overrides):
    """The task record as the harness writes it: `ticket` and a truncated
    composite `run_id`, with no run, task, phase or initiative key."""
    return {"run_id": "epic-x-5:seams:seams-t", "ticket": "seams-task", "review": {"verdict": "approve"}, "proposals": [], **overrides}


def _recover_dry_run(repo, tmp_path, record, capsys, work_dirs=("x/seams",)):
    sp.run(["git", "-C", str(repo), "branch", "epic/x/seams", "main"], check=True, env=_ENV)
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(record), encoding="utf-8")
    for d in work_dirs:
        (tmp_path / "work" / d).mkdir(parents=True)
        (tmp_path / "work" / d / "seams-task.md").write_text("---\nid: seams-task\nstate: approved\n---\n\nBody.\n", encoding="utf-8")
    rc = cli.main(["runs", "recover", "epic-x-5", "seams-task", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs"), "--dry-run"])
    return rc, capsys.readouterr().out


def test_cli_recover_resolves_a_record_with_only_the_on_disk_keys(repo, tmp_path, capsys):
    rc, out = _recover_dry_run(repo, tmp_path, _on_disk_record(), capsys)
    assert rc == 0, out
    assert "would merge agents/epic-x-5/seams-task into epic/x/seams" in out


def test_cli_recover_refuses_when_the_record_ticket_disagrees_with_its_filename(repo, tmp_path, capsys):
    rc, out = _recover_dry_run(repo, tmp_path, _on_disk_record(ticket="other-task"), capsys)
    assert rc == 2
    assert "disagrees with its filename" in out


def test_cli_recover_refuses_when_no_initiative_holds_the_task(repo, tmp_path, capsys):
    rc, out = _recover_dry_run(repo, tmp_path, _on_disk_record(), capsys, work_dirs=())
    assert rc == 2
    assert "no single initiative" in out


def test_cli_recover_refuses_when_two_initiatives_hold_the_task(repo, tmp_path, capsys):
    rc, out = _recover_dry_run(repo, tmp_path, _on_disk_record(), capsys, work_dirs=("x/seams", "y/seams"))
    assert rc == 2
    assert "no single initiative" in out


def test_initiative_of_takes_the_id_from_frontmatter_and_falls_back_to_the_stem(tmp_path):
    (tmp_path / "a/p").mkdir(parents=True)
    (tmp_path / "b/p").mkdir(parents=True)
    (tmp_path / "a/p/renamed.md").write_text("---\nid: from-frontmatter\n---\n\nBody.\n", encoding="utf-8")
    (tmp_path / "b/p/from-stem.md").write_text("---\nstate: approved\n---\n\nBody.\n", encoding="utf-8")
    assert cli._initiative_of(tmp_path, "from-frontmatter") == "a"
    assert cli._initiative_of(tmp_path, "renamed") is None
    assert cli._initiative_of(tmp_path, "from-stem") == "b"


def test_cli_recover_merges_and_closes_an_approved_item(repo, tmp_path, capsys):
    sp.run(["git", "-C", str(repo), "branch", "epic/x/seams", "main"], check=True, env=_ENV)
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_on_disk_record()), encoding="utf-8")
    item_dir = tmp_path / "work/x/seams"; item_dir.mkdir(parents=True)
    item_path = item_dir / "seams-task.md"
    item_path.write_text("---\nid: seams-task\nstate: approved\n---\n\nBody.\n", encoding="utf-8")
    rc = cli.main(["runs", "recover", "epic-x-5", "seams-task", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "epic/x/seams" in out
    assert item_path.read_text() == "---\nid: seams-task\nstate: done\n---\n\nBody.\n"


def test_cli_recover_dry_run_leaves_an_approved_item_byte_identical(repo, tmp_path, capsys):
    sp.run(["git", "-C", str(repo), "branch", "epic/x/seams", "agents/epic-x-5/seams-task"], check=True, env=_ENV)
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_on_disk_record()), encoding="utf-8")
    item_dir = tmp_path / "work/x/seams"; item_dir.mkdir(parents=True)
    item_path = item_dir / "seams-task.md"
    original = "---\nid: seams-task\nstate: approved\n---\n\nBody.\n"
    item_path.write_text(original, encoding="utf-8")
    rc = cli.main(["runs", "recover", "epic-x-5", "seams-task", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs"), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "already contains the commit" in out
    assert item_path.read_text() == original


def test_cli_recover_closes_an_approved_item_even_when_already_recovered(repo, tmp_path, capsys):
    sp.run(["git", "-C", str(repo), "branch", "epic/x/seams", "agents/epic-x-5/seams-task"], check=True, env=_ENV)
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_on_disk_record()), encoding="utf-8")
    item_dir = tmp_path / "work/x/seams"; item_dir.mkdir(parents=True)
    item_path = item_dir / "seams-task.md"
    item_path.write_text("---\nid: seams-task\nstate: approved\n---\n\nBody.\n", encoding="utf-8")
    rc = cli.main(["runs", "recover", "epic-x-5", "seams-task", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "already contains the commit" in out
    assert item_path.read_text() == "---\nid: seams-task\nstate: done\n---\n\nBody.\n"


def test_cli_recover_leaves_a_ready_item_ready_when_already_recovered(repo, tmp_path, capsys):
    sp.run(["git", "-C", str(repo), "branch", "epic/x/seams", "agents/epic-x-5/seams-task"], check=True, env=_ENV)
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_on_disk_record()), encoding="utf-8")
    item_dir = tmp_path / "work/x/seams"; item_dir.mkdir(parents=True)
    item_path = item_dir / "seams-task.md"
    original = "---\nid: seams-task\nstate: ready\n---\n\nBody.\n"
    item_path.write_text(original, encoding="utf-8")
    rc = cli.main(["runs", "recover", "epic-x-5", "seams-task", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "already contains the commit" in out
    assert "not moving to done" in out
    assert item_path.read_text() == original


def _apply_gated(repo, tmp_path, monkeypatch, *gate):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    (tmp_path / "runs/policy.tracker.json").write_text('{"tracker": "github-projects"}', encoding="utf-8")
    ran = []
    monkeypatch.setattr(cli, "_execute_land_step", lambda _repo, step, _forge=None: (ran.append(step["kind"]) or True, "https://x/pull/7"))
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--runs-dir", str(tmp_path / "runs"), *gate])
    return rc, ran


def test_cli_apply_at_phase_level_prints_the_truncation_line_and_exits_3(repo, tmp_path, capsys, monkeypatch):
    rc, ran = _apply_gated(repo, tmp_path, monkeypatch, "--gate", "phase")
    assert rc == 3
    assert ran[-1] == "wait_checks"
    assert capsys.readouterr().out.splitlines()[-1] == (
        "gate: phase stopped after wait_checks; pull request https://x/pull/7 left open, unmerged"
    )


def test_cli_apply_at_full_level_merges_and_exits_0(repo, tmp_path, capsys, monkeypatch):
    rc, ran = _apply_gated(repo, tmp_path, monkeypatch, "--gate", "full")
    assert rc == 0
    assert ran == _PRESYNCED + ["route_sync"]
    assert "left open" not in capsys.readouterr().out


def _apply_presynced(repo, tmp_path, monkeypatch, sync_writes_issue):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    item = tmp_path / "work/x/seams/seams-task.md"; item.parent.mkdir(parents=True)
    item.write_text("---\nid: seams-task\nstate: approved\n---\nBody.\n", encoding="utf-8")
    (tmp_path / "runs/policy.tracker.json").write_text('{"tracker": "github-projects"}', encoding="utf-8")
    ran = []

    def fake(_repo, step, _forge=None):
        ran.append(step)
        if step["kind"] == "route_sync" and step.get("before") and sync_writes_issue:
            item.write_text("---\nid: seams-task\nstate: approved\nissue: 9\n---\nBody.\n", encoding="utf-8")
        return True, "https://x/pull/7"
    monkeypatch.setattr(cli, "_execute_land_step", fake)
    cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--runs-dir", str(tmp_path / "runs")])
    return ran


def test_cli_apply_syncs_before_the_pr_and_the_body_carries_the_new_issue(repo, tmp_path, monkeypatch):
    ran = _apply_presynced(repo, tmp_path, monkeypatch, sync_writes_issue=True)
    assert [s["kind"] for s in ran][3:6] == ["push", "route_sync", "pr_create"]
    assert ran[4]["workspace"] == str(tmp_path) and ran[4]["item"] == "seams-task"
    assert ran[5]["body"].endswith("\n\nCloses #9")


def test_cli_apply_opens_the_pr_without_closes_when_the_sync_wrote_no_issue(repo, tmp_path, monkeypatch):
    ran = _apply_presynced(repo, tmp_path, monkeypatch, sync_writes_issue=False)
    assert ran[-1]["kind"] == "pr_create" and "Closes" not in ran[-1]["body"]


def test_execute_presync_failure_says_the_pr_opens_without_closes_and_never_fails_the_land(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_route_sync", lambda ns: 1)
    ok, detail = cli._execute_land_step(tmp_path, {**_sync_step(tmp_path), "before": "pr_create"})
    assert ok is True and detail == "sync of seams-task failed before the PR (exit 1); the PR opens without Closes"


def test_cli_apply_with_no_policy_stops_at_ticket_after_pr_create(repo, tmp_path, monkeypatch):
    rc, ran = _apply_gated(repo, tmp_path, monkeypatch)
    assert (rc, ran[-1]) == (3, "pr_create")


def test_cli_apply_refuses_on_a_dirty_checkout(repo, tmp_path, capsys):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    (repo / "f").write_text("dirty")
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--runs-dir", str(tmp_path / "runs")])
    assert rc == 2
    assert "dirty" in capsys.readouterr().out


def test_cli_apply_refuses_when_the_pr_branch_already_exists(repo, tmp_path, capsys):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    sp.run(["git", "-C", str(repo), "branch", "pr/seams-task"], check=True, capture_output=True)
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--runs-dir", str(tmp_path / "runs")])
    assert rc == 2
    assert "already exists" in capsys.readouterr().out


def test_cli_apply_refuses_a_checks_launch_failure_before_push(repo, tmp_path, capsys, monkeypatch):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    calls = []
    real_run = cli.subprocess.run

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:1] == ["pytest"]:
            raise FileNotFoundError(2, "No such file or directory: 'pytest'")
        return real_run(argv, **kw)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--runs-dir", str(tmp_path / "runs")])
    out = capsys.readouterr().out
    assert rc == 2
    assert "refuse checks: tests:" in out
    assert not any("push" in c for c in calls)


# --- §1 paths: runs/ resolves against workspace_dir, --runs-dir overrides, refusal names dir+count ---

def test_land_default_resolves_against_the_profiles_workspace_dir(repo, tmp_path, capsys, monkeypatch):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(f"workspace_dir: {tmp_path}\n", encoding="utf-8")
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--profile", str(profile_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"kind": "pick_branch"' in out


def test_land_runs_dir_flag_overrides_the_profile(repo, tmp_path, capsys):
    empty_workspace = tmp_path / "empty"; empty_workspace.mkdir()
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(f"workspace_dir: {empty_workspace}\n", encoding="utf-8")
    real_runs = tmp_path / "elsewhere/runs"
    task_dir = real_runs / "epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--profile", str(profile_path), "--runs-dir", str(real_runs)])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"kind": "pick_branch"' in out


def test_land_refusal_names_the_absolute_dir_and_the_count(repo, tmp_path, capsys):
    runs_dir = tmp_path / "runs"
    task_dir = runs_dir / "epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--runs-dir", str(runs_dir)])
    out = capsys.readouterr().out
    assert rc == 2
    expected = task_dir.parent.resolve()
    assert f"land: looked in {expected}, found 0 task records, expected 1" in out

    # Two records, but in two different phase directories — no single phase
    # holds more than one, so this stays ambiguous for task mode rather than
    # auto-selecting a phase.
    (task_dir / "a.json").write_text(json.dumps(_record()), encoding="utf-8")
    other_dir = runs_dir / "epic-x-5/tasks/other"; other_dir.mkdir(parents=True)
    (other_dir / "b.json").write_text(json.dumps(_record()), encoding="utf-8")
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--runs-dir", str(runs_dir)])
    out = capsys.readouterr().out
    assert rc == 2
    assert f"land: looked in {expected}, found 2 task records, expected 1" in out


def test_cli_apply_checks_the_leader_guard_against_the_resolved_runs_dir(repo, tmp_path, capsys):
    runs_dir = tmp_path / "runs"
    task_dir = runs_dir / "epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    now = datetime.datetime.now(datetime.UTC).isoformat()
    chair.write(runs_dir, {"session": "loop-a", "pid": 999999, "host": "some-other-host", "taken_at": now, "heartbeat_at": now})
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--runs-dir", str(runs_dir)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "held by loop-a" in out


def test_wait_decision_retries_only_while_no_check_has_registered():
    from agent_tools.land import wait_decision

    assert wait_decision(0, "all checks pass", 0, 300) == "green"
    assert wait_decision(1, "no checks reported on the 'pr/x' branch", 5, 300) == "retry"
    assert wait_decision(1, "no checks reported on the 'pr/x' branch", 301, 300) == "timeout"
    assert wait_decision(1, "test: fail", 5, 300) == "failed"


def test_unanimous_approval_lands_without_an_arbitration_verdict():
    branches = {"agents/epic-x-5/seams-task": ["Add seams module"]}
    record = _record(arbitration={}, review={"verdict": "approve"}, adversary={"verdict": "approve"})
    assert land.land_plan(record, branches, "main")[0]["kind"] != "refuse"


def test_no_arbitration_and_a_dissenting_reviewer_still_refuses():
    record = _record(arbitration={}, review={"verdict": "approve"}, adversary={"verdict": "revise"})
    assert land.land_plan(record, {}, "main") == [
        {"kind": "refuse", "reason": "no arbitration, and the reviewers were ['approve', 'revise']"}
    ]


# --- resume: an existing pr/<task> branch with the cherry-picked tree is reused ---

_CHERRY = {"kind": "cherry_pick", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module",
           "onto": "pr/seams-task", "from": "main"}


def test_resume_decision_is_fresh_when_neither_side_exists():
    assert land.resume_decision("T", None, None, []) == {"kind": "fresh"}


def test_resume_decision_resumes_on_a_matching_local_branch_alone():
    assert land.resume_decision("T", "T", None, []) == {"kind": "resume", "local": True, "remote": False}


def test_resume_decision_resumes_on_a_matching_remote_branch_alone():
    assert land.resume_decision("T", None, "T", []) == {"kind": "resume", "local": False, "remote": True}


def test_resume_decision_resumes_when_both_sides_match():
    assert land.resume_decision("T", "T", "T", []) == {"kind": "resume", "local": True, "remote": True}


def test_resume_decision_refuses_a_differing_side_naming_both_trees():
    assert land.resume_decision("T", "T", "U", []) == {
        "kind": "refuse", "reason": "remote tree U differs from the cherry-picked tree T"}


def test_resume_decision_refuses_an_open_pr_naming_its_number():
    assert land.resume_decision("T", "T", "T", [7]) == {"kind": "refuse", "reason": "open pull request #7 points at the branch"}


def test_resume_steps_swaps_only_the_cherry_pick_and_leaves_a_non_resume_alone():
    steps = land.land_plan(_record(), {"agents/epic-x-5/seams-task": ["Add seams module"]}, "main")
    resumed = land.resume_steps(steps, {"kind": "resume", "local": True, "remote": False}, "pr/seams-task")
    assert [s["kind"] for s in resumed] == [k if k != "cherry_pick" else "reuse_branch" for k in _STEP_ORDER]
    assert resumed[1] == {"kind": "reuse_branch", "branch": "pr/seams-task", "local": True, "remote": False}
    assert land.resume_steps(steps, {"kind": "fresh"}, "pr/seams-task") == steps


def _apply_resume(repo, tmp_path, monkeypatch, prs=()):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    (tmp_path / "runs/policy.tracker.json").write_text('{"tracker": "github-projects"}', encoding="utf-8")
    ran = []
    monkeypatch.setattr(cli, "_execute_land_step", lambda _repo, step, _forge=None: (ran.append(step["kind"]) or True, "https://x/pull/7"))
    monkeypatch.setattr(cli, "_open_prs_for", lambda _repo, _branch, _forge=None: list(prs))
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--runs-dir", str(tmp_path / "runs"), "--gate", "full"])
    return rc, ran


def test_cli_apply_resumes_on_a_local_pr_branch_with_the_cherry_picked_tree(repo, tmp_path, capsys, monkeypatch):
    sp.run(["git", "-C", str(repo), "branch", "pr/seams-task", "agents/epic-x-5/seams-task"], check=True, env=_ENV)
    rc, ran = _apply_resume(repo, tmp_path, monkeypatch)
    assert rc == 0, capsys.readouterr().out
    assert ran[:10] == [k if k != "cherry_pick" else "reuse_branch" for k in _PRESYNCED]


def test_cli_apply_refuses_a_matching_pr_branch_that_has_an_open_pr(repo, tmp_path, capsys, monkeypatch):
    sp.run(["git", "-C", str(repo), "branch", "pr/seams-task", "agents/epic-x-5/seams-task"], check=True, env=_ENV)
    rc, ran = _apply_resume(repo, tmp_path, monkeypatch, prs=[7])
    assert (rc, ran) == (2, [])
    assert "open pull request #7" in capsys.readouterr().out


def test_cli_apply_refuses_a_differing_pr_branch_naming_the_diff(repo, tmp_path, capsys, monkeypatch):
    sp.run(["git", "-C", str(repo), "branch", "pr/seams-task", "main"], check=True, env=_ENV)
    rc, ran = _apply_resume(repo, tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert (rc, ran) == (2, [])
    assert "already exists" in out and "local tree" in out and "files differing: f" in out


def test_execute_reuse_branch_checks_out_the_existing_local_branch(repo):
    sp.run(["git", "-C", str(repo), "branch", "pr/seams-task", "agents/epic-x-5/seams-task"], check=True, env=_ENV)
    ok, detail = cli._execute_land_step(repo, {"kind": "reuse_branch", "branch": "pr/seams-task", "local": True, "remote": False})
    assert (ok, detail) == (True, "reusing pr/seams-task")
    assert cli._git_out(cli._land_worktree(repo, "pr/seams-task"), "rev-parse", "--abbrev-ref", "HEAD") == "pr/seams-task"
    assert cli._git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def _with_origin_holding_the_pr_branch(repo, tmp_path):
    origin = tmp_path / "origin.git"
    sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    sp.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    sp.run(["git", "-C", str(repo), "push", "-q", "origin", "agents/epic-x-5/seams-task:refs/heads/pr/seams-task"], check=True, env=_ENV)


def test_execute_reuse_branch_creates_the_local_branch_from_origin_when_only_the_remote_has_it(repo, tmp_path):
    _with_origin_holding_the_pr_branch(repo, tmp_path)
    sp.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True, env=_ENV)
    ok, detail = cli._execute_land_step(repo, {"kind": "reuse_branch", "branch": "pr/seams-task", "local": False, "remote": True})
    assert (ok, detail) == (True, "reusing pr/seams-task")
    assert cli._git_out(cli._land_worktree(repo, "pr/seams-task"), "rev-parse", "--abbrev-ref", "HEAD") == "pr/seams-task"
    assert cli._git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_land_resume_finds_a_remote_only_branch_with_the_cherry_picked_tree(repo, tmp_path, monkeypatch):
    _with_origin_holding_the_pr_branch(repo, tmp_path)
    monkeypatch.setattr(cli, "_open_prs_for", lambda _repo, _branch, _forge=None: [])
    assert cli._land_resume(repo, _CHERRY) == {"kind": "resume", "local": False, "remote": True}


def _run_row(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_rest_checks_argvs_are_paginated_gh_api_calls_for_check_runs_and_status():
    runs, status = land.rest_checks_argvs("abc123")
    assert runs == ["gh", "api", "--paginate", "repos/{owner}/{repo}/commits/abc123/check-runs?per_page=100"]
    assert status == ["gh", "api", "--paginate", "repos/{owner}/{repo}/commits/abc123/status?per_page=100"]


def test_merge_pages_joins_back_to_back_pages_and_refuses_garbage():
    text = '{"total_count": 3, "check_runs": [1, 2]}\n{"total_count": 3, "check_runs": [3]}'
    assert land.merge_pages(text, "check_runs") == {"total_count": 3, "check_runs": [1, 2, 3]}
    assert land.merge_pages("", "check_runs") is None
    assert land.merge_pages('{"check_runs": []} not json', "check_runs") is None


def test_check_poll_result_maps_green_failed_pending_and_empty():
    green_status = {"state": "success", "statuses": [{"context": "ci", "state": "success"}]}
    assert land.check_poll_result({"check_runs": [_run_row("a"), _run_row("b", conclusion="skipped")]}, green_status) == (0, "")
    mixed = {"check_runs": [_run_row("a", conclusion="failure"), _run_row("b", "in_progress", None)]}
    assert land.check_poll_result(mixed, {"statuses": []}) == (1, "failing checks: a")
    assert land.check_poll_result({"check_runs": []}, {"statuses": [{"context": "ci", "state": "error"}]}) == (1, "failing checks: ci")
    queued = {"check_runs": [_run_row("a", "queued", None)]}
    assert land.check_poll_result(queued, {"state": "pending", "statuses": []}) == (land.PENDING_RC, "checks pending: a")
    assert land.check_poll_result({"check_runs": []}, {"state": "pending", "statuses": []}) == (1, "no checks reported")


def test_check_poll_result_is_never_green_when_fewer_rows_than_total_count_were_read():
    hundred_green = {"total_count": 101, "check_runs": [_run_row(f"r{i}") for i in range(100)]}
    assert land.check_poll_result(hundred_green, {"statuses": []}) == (land.PENDING_RC, "checks pending: 100 of 101 check runs read")
    statuses = {"total_count": 2, "state": "success", "statuses": [{"context": "ci", "state": "success"}]}
    assert land.check_poll_result({"check_runs": []}, statuses)[0] == land.PENDING_RC
    combined_failure = {"total_count": 101, "state": "failure", "statuses": [{"context": "ci", "state": "success"}]}
    assert land.check_poll_result({"check_runs": []}, combined_failure) == (1, "failing checks: combined status")


def test_wait_decision_retries_pending_by_returncode_alone_and_reads_no_phrase():
    assert land.wait_decision(land.PENDING_RC, "", 5, 180) == "retry"
    assert land.wait_decision(land.PENDING_RC, "", 999999, 180) == "retry"
    assert land.wait_decision(1, "failing checks: a", 5, 300) == "failed"
    assert land.wait_decision(1, "1 failing, 1 successful, and 1 pending checks", 5, 300) == "failed"
    assert land.wait_decision(8, "checks pending: a", 5, 300) == "failed"


def test_unreadable_poll_retries_below_the_limit_and_fails_at_it():
    assert land.unreadable_poll(1, "HTTP 502")[0] == land.PENDING_RC
    assert land.unreadable_poll(land.POLL_ERROR_LIMIT, "HTTP 502") == (1, "checks unreadable 5 polls in a row: HTTP 502")


def test_poll_backoff_doubles_from_thirty_seconds_and_caps():
    assert [land.poll_backoff_s(n) for n in (1, 2, 3, 4, 5)] == [30.0, 60.0, 120.0, 240.0, 240.0]


def _rest_run(calls, check_runs_answers):
    answers = iter(check_runs_answers)

    def run(argv, **kw):
        calls.append(argv)
        if argv[0] == "git":
            return sp.CompletedProcess(argv, 0, "abc123\n", "")
        if "/check-runs" not in argv[-1]:
            return sp.CompletedProcess(argv, 0, json.dumps({"total_count": 0, "statuses": []}), "")
        answer = next(answers)
        return answer if isinstance(answer, sp.CompletedProcess) else sp.CompletedProcess(argv, 0, json.dumps(answer), "")
    return run


_GREEN = {"total_count": 1, "check_runs": [_run_row("a")]}
_RATE_LIMITED = sp.CompletedProcess([], 1, "", "HTTP 403: secondary rate limit")


@pytest.mark.parametrize("answers, expected", [
    ([_GREEN], (True, "green")),
    ([{"total_count": 1, "check_runs": [_run_row("a", conclusion="failure")]}], (False, "failing checks: a")),
    ([{"total_count": 1, "check_runs": [_run_row("a", "queued", None)]}, _GREEN], (True, "green")),
    ([_RATE_LIMITED, _RATE_LIMITED, _GREEN], (True, "green")),
    ([_RATE_LIMITED] * 5, (False, "checks unreadable 5 polls in a row: HTTP 403: secondary rate limit")),
])
def test_wait_checks_polls_rest_and_never_gh_pr_checks(monkeypatch, tmp_path, answers, expected):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _rest_run(calls, answers))
    assert cli._wait_checks(tmp_path, 180.0, sleep=lambda s: None) == expected
    assert any(c[:3] == ["gh", "api", "--paginate"] and "/check-runs" in c[-1] for c in calls)
    assert not any(c[:3] == ["gh", "pr", "checks"] for c in calls)


def test_wait_checks_waits_out_a_long_pending_run_as_gh_watch_did(monkeypatch, tmp_path):
    t = [0.0]
    queued = {"total_count": 1, "check_runs": [_run_row("a", "queued", None)]}
    monkeypatch.setattr(cli.subprocess, "run", _rest_run([], [queued] * 500 + [_GREEN]))
    result = cli._wait_checks(tmp_path, 180.0, sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: t[0])
    assert result == (True, "green") and t[0] == 500 * 15


def test_wait_checks_backs_off_after_each_unreadable_poll_before_failing(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(cli.subprocess, "run", _rest_run([], [_RATE_LIMITED] * 5))
    ok, _ = cli._wait_checks(tmp_path, 180.0, sleep=slept.append)
    assert ok is False
    assert slept == [30.0, 15, 60.0, 15, 120.0, 15, 240.0, 15]


def test_wait_checks_reads_the_sha_of_the_repos_own_head_and_runs_gh_there(monkeypatch, tmp_path):
    real_run = sp.run
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "a@b.c"], ["config", "user.name", "n"],
                 ["commit", "--allow-empty", "-qm", "one"], ["checkout", "-qb", "pr-branch"],
                 ["commit", "--allow-empty", "-qm", "two"]):
        real_run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    def sha(ref):
        return real_run(["git", "-C", str(tmp_path), "rev-parse", ref], capture_output=True, text=True).stdout.strip()

    head, main = sha("HEAD"), sha("main")
    gh_calls = []

    def run(argv, **kw):
        if argv[0] == "git":
            return real_run(argv, **kw)
        gh_calls.append((argv, kw.get("cwd")))
        body = _GREEN if "/check-runs" in argv[-1] else {"total_count": 0, "statuses": []}
        return sp.CompletedProcess(argv, 0, json.dumps(body), "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._wait_checks(tmp_path, 180.0, sleep=lambda s: None) == (True, "green")
    assert head != main and len(gh_calls) == 2
    assert all(head in argv[-1] and main not in argv[-1] and cwd == tmp_path for argv, cwd in gh_calls)
    gh_calls.clear()
    assert cli._wait_checks(tmp_path, 180.0, sleep=lambda s: None, ref="main") == (True, "green")
    assert len(gh_calls) == 2
    assert all(main in argv[-1] and head not in argv[-1] for argv, _ in gh_calls)


# --- land.jsonl: one row per applied land that reached its steps ---

def test_land_log_row_is_the_six_keys_and_an_empty_pr_is_null():
    row = land.land_log_row("2026-09-24T10:00:00+00:00", "epic-x-5", "seams-task", ["push", "merge"], 0, "https://x/pull/7")
    assert row == {"ts": "2026-09-24T10:00:00+00:00", "run": "epic-x-5", "task": "seams-task",
                   "steps_reached": ["push", "merge"], "exit": 0, "pr": "https://x/pull/7"}
    assert land.land_log_row("t", "r", None, [], 2, "")["pr"] is None


def _land_log(tmp_path):
    return [json.loads(line) for line in (tmp_path / "runs/land.jsonl").read_text(encoding="utf-8").splitlines()]


def test_a_successful_land_writes_one_row_reaching_merge_with_exit_0(repo, tmp_path, monkeypatch):
    rc, ran = _apply_gated(repo, tmp_path, monkeypatch, "--gate", "full")
    (row,) = _land_log(tmp_path)
    assert rc == 0
    assert row["steps_reached"] == ran and "merge" in row["steps_reached"]
    assert (row["exit"], row["run"], row["task"], row["pr"]) == (0, "epic-x-5", "seams-task", "https://x/pull/7")


def test_a_refused_land_writes_a_row_with_its_exit_code_and_no_merge_and_a_rerun_appends(repo, tmp_path):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record(proposals=[])), encoding="utf-8")
    argv = ["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--runs-dir", str(tmp_path / "runs")]
    assert cli.main(argv) == 2
    assert cli.main(argv) == 2
    assert [(r["exit"], r["steps_reached"], r["pr"]) for r in _land_log(tmp_path)] == [(2, [], None)] * 2


def test_a_land_with_no_tracker_configured_plans_no_route_sync(repo, tmp_path, capsys):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\n", encoding="utf-8")
    argv = ["runs", "land", "epic-x-5", "--repo", str(repo), "--profile", str(profile)]
    assert cli.main(argv) == 0
    assert "route_sync" not in [s["kind"] for s in json.loads(capsys.readouterr().out)]
    profile.write_text(f"workspace_dir: {tmp_path}\ntracker: github-projects\n", encoding="utf-8")
    assert cli.main(argv) == 0
    assert "route_sync" in [s["kind"] for s in json.loads(capsys.readouterr().out)]


def test_a_lands_route_sync_step_reads_the_tracker_from_the_lands_own_profile_and_runs_dir(repo, tmp_path, capsys, monkeypatch):
    runs = tmp_path / "custom-runs"
    task_dir = runs / "epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\ntracker: github-projects\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_TOOLS_PROFILE", str(tmp_path / "no-default-profile.yaml"))
    assert cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--profile", str(profile), "--runs-dir", str(runs),
                     "--gate", "full"]) == 0
    out = capsys.readouterr().out
    step = next(s for s in json.loads(out[out.index("["):]) if s["kind"] == "route_sync" and not s.get("before"))
    assert (step["profile"], step["runs_dir"]) == (str(profile), str(runs))
    seen = []
    monkeypatch.setattr(cli, "_route_sync", lambda ns: seen.append(ns.profile) or 0)
    assert cli._execute_land_step(repo, step) == (True, "synced seams-task")
    assert seen == [str(profile)]
    (runs / "policy.tracker.json").write_text('{"tracker": "none"}', encoding="utf-8")
    assert cli._execute_land_step(repo, step) == (True, "sync of seams-task skipped after the merge: tracker is none")
    assert seen == [str(profile)]


def test_a_dry_run_land_refuses_an_unreadable_profile_because_the_plan_reads_its_tracker(repo, tmp_path, capsys):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text("no_such_key: x\n", encoding="utf-8")
    argv = ["runs", "land", "epic-x-5", "--repo", str(repo), "--profile", str(profile), "--runs-dir", str(tmp_path / "runs")]
    assert cli.main(argv) == 2
    assert capsys.readouterr().out.startswith("land: profile unreadable:")


def test_a_dry_run_writes_no_land_log(repo, tmp_path):
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    assert cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")]) == 0
    assert not (tmp_path / "runs/land.jsonl").exists()


# --- the profile's forge chooses where the land's push, PR, checks and merge go ---

def _forge_land(tmp_path, forge_line=""):
    """An approved task record and open work item under `tmp_path`, plus a profile; returns the land argv."""
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"; task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    (tmp_path / "runs/policy.tracker.json").write_text('{"tracker": "none"}', encoding="utf-8")
    item = tmp_path / "work/x/seams/seams-task.md"; item.parent.mkdir(parents=True)
    item.write_text("---\nid: seams-task\nstate: approved\n---\nBody.\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"workspace_dir: {tmp_path}\n{forge_line}", encoding="utf-8")
    return ["--apply", "--profile", str(profile), "--runs-dir", str(tmp_path / "runs")]


def test_a_land_with_no_forge_in_the_profile_fast_forwards_main_and_calls_no_gh(repo, tmp_path, capsys, monkeypatch):
    argv = _forge_land(tmp_path)
    real_run = cli.subprocess.run

    def no_gh(cmd, *a, **kw):
        assert cmd[:1] != ["gh"], cmd
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(cli.subprocess, "run", no_gh)
    monkeypatch.setattr(cli, "_run_checks", lambda checks, cwd: (True, "1 checks passed"))
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--gate", "full", *argv])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "forge: local" in out.splitlines()
    assert cli._git_out(repo, "log", "-1", "--format=%s", "main") == "Add seams module"
    assert cli._git_out(repo, "show", "main:f") == "y"
    assert "pr/seams-task" not in cleanup.git_branches(repo)


# --- a task land builds its pr branch in its own worktree; the checkout's HEAD never moves ---

def _head(repo):
    return cli._git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _land_worktrees(repo):
    return [line for line in cli._git_out(repo, "worktree", "list").splitlines() if "cox-land-" in line]


def _land(repo, tmp_path, checks, capsys):
    """Run a full-gate task land with the local forge; `checks` stands in for `_run_checks`."""
    profile = tmp_path / "profile.yaml"
    argv = ["--apply", "--profile", str(profile), "--runs-dir", str(tmp_path / "runs")] if profile.exists() else _forge_land(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_run_checks", checks)
        rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--gate", "full", *argv])
    return rc, capsys.readouterr().out


@pytest.mark.parametrize("start", ["main", "side"])
def test_a_full_task_land_leaves_the_checkouts_head_where_it_was(repo, tmp_path, capsys, start):
    if start == "side":
        sp.run(["git", "-C", str(repo), "checkout", "-qb", "side"], check=True, env=_ENV)
    seen = []
    rc, out = _land(repo, tmp_path, lambda checks, cwd: (seen.append(cwd) or True, "1 checks passed"), capsys)
    assert rc == 0, out
    assert _head(repo) == start
    assert cli._git_out(repo, "show", "main:f") == "y"
    assert seen == [cli._land_worktree(repo, "pr/seams-task")]
    assert _land_worktrees(repo) == [] and "pr/seams-task" not in cleanup.git_branches(repo)


def test_a_land_whose_checks_fail_keeps_the_pr_branch_and_leaves_head_and_no_worktree(repo, tmp_path, capsys):
    rc, out = _land(repo, tmp_path, lambda checks, cwd: (False, "tests: boom"), capsys)
    assert rc == 1, out
    assert _head(repo) == "main"
    assert "pr/seams-task" in cleanup.git_branches(repo)
    assert cli._git_out(repo, "show", "pr/seams-task:f") == "y"
    assert _land_worktrees(repo) == []
    assert cli._git_out(repo, "show", "main:f") == "x"


@pytest.mark.parametrize("start", ["main", "side"])
def test_a_resumed_land_works_the_same_way(repo, tmp_path, capsys, start):
    rc, out = _land(repo, tmp_path, lambda checks, cwd: (False, "tests: boom"), capsys)
    assert rc == 1, out
    if start == "side":
        sp.run(["git", "-C", str(repo), "checkout", "-qb", "side"], check=True, env=_ENV)
    rc, out = _land(repo, tmp_path, lambda checks, cwd: (True, "1 checks passed"), capsys)
    assert rc == 0, out
    assert "reuse_branch: reusing pr/seams-task" in out
    assert _head(repo) == start
    assert cli._git_out(repo, "show", "main:f") == "y"
    assert _land_worktrees(repo) == []


def test_a_resumed_land_whose_checks_fail_again_keeps_the_branch_and_head(repo, tmp_path, capsys):
    _land(repo, tmp_path, lambda checks, cwd: (False, "tests: boom"), capsys)
    rc, out = _land(repo, tmp_path, lambda checks, cwd: (False, "tests: boom"), capsys)
    assert rc == 1, out
    assert "reuse_branch: reusing pr/seams-task" in out
    assert _head(repo) == "main"
    assert "pr/seams-task" in cleanup.git_branches(repo)
    assert _land_worktrees(repo) == []


def test_a_dry_run_land_touches_no_disk_for_the_land_worktree(repo, tmp_path):
    (tmp_path / "runs/epic-x-5/tasks/seams").mkdir(parents=True)
    (tmp_path / "runs/epic-x-5/tasks/seams/seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    assert cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--runs-dir", str(tmp_path / "runs")]) == 0
    assert not cli._land_worktree(repo, "pr/seams-task").exists()


def test_a_profile_naming_github_routes_pr_create_to_the_github_forge(repo, tmp_path, capsys, monkeypatch):
    argv = _forge_land(tmp_path, "forge: github\n")
    calls = []
    monkeypatch.setattr(cli, "_run_checks", lambda checks, cwd: (True, "1 checks passed"))
    monkeypatch.setattr(forge_github, "find_open_prs", lambda repo, branch: [])
    monkeypatch.setattr(forge_github, "push", lambda repo, branch: (calls.append("push") or True, branch))
    monkeypatch.setattr(forge_github, "open_pr", lambda repo, title, body, **refs: (calls.append(("open_pr", refs)) or True, "https://x/pull/7"))
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), *argv])
    out = capsys.readouterr().out
    assert rc == 3, out
    assert calls == ["push", ("open_pr", {"head": "pr/seams-task", "base": "main"})]
    assert "forge: github" in out.splitlines()
    assert "pr_create: https://x/pull/7" in out


def test_a_profile_naming_an_unknown_forge_refuses_with_exit_2(repo, tmp_path, capsys):
    argv = _forge_land(tmp_path, "forge: nope\n")
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), *argv])
    assert rc == 2
    assert capsys.readouterr().out.splitlines()[-1] == "land: no forge named nope (built in: local, github)"
    assert "pr/seams-task" not in cleanup.git_branches(repo)


def test_a_github_land_hands_the_forge_the_planned_head_base_and_checks_ref(repo, tmp_path, capsys, monkeypatch):
    argv = _forge_land(tmp_path, "forge: github\n")
    calls = []
    monkeypatch.setattr(cli, "_run_checks", lambda checks, cwd: (True, "1 checks passed"))
    monkeypatch.setattr(forge_github, "find_open_prs", lambda repo, branch: [])
    monkeypatch.setattr(forge_github, "push", lambda repo, branch: (True, branch))
    monkeypatch.setattr(forge_github, "open_pr", lambda repo, title, body, *, head=None, base=None:
                        (calls.append(("open_pr", head, base)) or True, "https://x/pull/7"))
    monkeypatch.setattr(forge_github, "wait_checks", lambda repo, timeout_s, *, ref="HEAD":
                        (calls.append(("wait_checks", ref)) or True, "green"))
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--gate", "phase", *argv])
    out = capsys.readouterr().out
    assert rc == 3, out
    assert calls == [("open_pr", "pr/seams-task", "main"), ("wait_checks", "pr/seams-task")]


def test_a_land_refuses_before_any_step_when_the_forge_predates_explicit_refs(repo, tmp_path, capsys, monkeypatch):
    argv = _forge_land(tmp_path, "forge: github\n")
    pushed = []
    monkeypatch.setattr(forge_github, "find_open_prs", lambda repo, branch: [])
    monkeypatch.setattr(forge_github, "push", lambda repo, branch: (pushed.append(branch) or True, branch))
    monkeypatch.setattr(forge_github, "open_pr", lambda repo, title, body: (True, "https://x/pull/7"))
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), *argv])
    out = capsys.readouterr().out
    assert rc == 2 and pushed == []
    assert "land: refusing, forge github does not accept open_pr(head), open_pr(base)" in out


def test_the_ticket_plans_merge_step_carries_what_the_local_forge_reads():
    steps = land.land_plan(_record(), {"agents/epic-x-5/seams-task": ["Add seams module"]}, "trunk")
    assert next(s for s in steps if s["kind"] == "merge") == {
        "kind": "merge", "squash": True, "delete_branch": True,
        "branch": "pr/seams-task", "default_branch": "trunk", "subject": "Add seams module",
    }


def test_the_phase_plans_merge_step_carries_what_the_local_forge_reads():
    phase_record = {"run": "epic-x-5", "phase": "seams", "initiative": "x"}
    steps = land.land_plan(phase_record, {}, "trunk", items=[{"id": "seams-task", "status": "done"}],
                           task_records=[_record(status="done")])
    assert next(s for s in steps if s["kind"] == "merge") == {
        "kind": "merge", "squash": True, "delete_branch": True,
        "branch": "epic/x/seams", "default_branch": "trunk", "subject": "epic x: seams",
    }


def test_the_ticket_plans_pr_and_checks_steps_name_their_branch_and_base():
    steps = land.land_plan(_record(), {"agents/epic-x-5/seams-task": ["Add seams module"]}, "trunk")
    pr = next(s for s in steps if s["kind"] == "pr_create")
    assert (pr["head"], pr["base"]) == ("pr/seams-task", "trunk")
    assert next(s for s in steps if s["kind"] == "wait_checks") == {"kind": "wait_checks", "branch": "pr/seams-task"}


def test_the_phase_plans_pr_and_checks_steps_name_their_branch_and_base():
    phase_record = {"run": "epic-x-5", "phase": "seams", "initiative": "x"}
    steps = land.land_plan(phase_record, {}, "trunk", items=[{"id": "seams-task", "status": "done"}],
                           task_records=[_record(status="done")])
    pr = next(s for s in steps if s["kind"] == "pr_create")
    assert (pr["head"], pr["base"]) == ("epic/x/seams", "trunk")
    assert next(s for s in steps if s["kind"] == "wait_checks") == {"kind": "wait_checks", "branch": "epic/x/seams"}


def test_land_phase_record_reads_the_store_manifest_when_no_file_exists(tmp_path):
    runs_table(tmp_path, run_row("epic-x-5"))
    phases_table(tmp_path, with_record("epic-x-5", "seams", "2026-09-25T04:40:00+00:00"), with_record("epic-x-5", "other", "2026-09-25T04:41:00+00:00"))
    record, tasks, paths, _ = cli._land_phase_record(tmp_path, "epic-x-5", "seams")
    assert not (tmp_path / "epic-x-5:seams.json").exists()
    assert record["run_id"] == "epic-x-5:seams" and record["phase"] == "seams" and (tasks, paths) == ([], {})


def test_land_phase_record_names_the_missing_file_when_neither_file_nor_store_has_it(tmp_path):
    record, tasks, paths, searched = cli._land_phase_record(tmp_path, "epic-x-5", "seams")
    assert (record, tasks, paths) == (None, [], {}) and searched == str((tmp_path / "epic-x-5:seams.json").resolve())


def test_a_task_lands_checks_find_the_repos_venv_in_the_land_worktree(repo, tmp_path):
    """The land's default check is `.venv/bin/python -m pytest -q`, relative to where it runs."""
    bin_dir = repo / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    check = bin_dir / "check"
    check.write_text(f"#!/bin/sh\npwd > {tmp_path / 'ran-in'}\n", encoding="utf-8")
    check.chmod(0o755)
    ok, detail = cli._execute_land_step(repo, {
        "kind": "cherry_pick", "branch": "agents/epic-x-5/seams-task", "commit_subject": "Add seams module",
        "onto": "pr/seams-task", "from": "main",
    })
    assert ok, detail
    ok, detail = cli._execute_land_step(repo, {"kind": "checks", "checks": [("tests", [".venv/bin/check"])], "worktree_of": "pr/seams-task"})
    assert ok, detail
    assert (tmp_path / "ran-in").read_text().strip() == str(cli._land_worktree(repo, "pr/seams-task"))


# --- resolve_add_add_conflicts: pure diff3 add/add hunk resolver ---


def test_resolve_add_add_conflicts_resolves_empty_base_to_ours_then_theirs():
    text = "<<<<<<< HEAD\nours line\n||||||| base\n=======\ntheirs line\n>>>>>>> theirs\n"
    assert land.resolve_add_add_conflicts(text) == "ours line\ntheirs line\n"


def test_resolve_add_add_conflicts_refuses_a_non_empty_base():
    text = "<<<<<<< HEAD\nours line\n||||||| base\nbase line\n=======\ntheirs line\n>>>>>>> theirs\n"
    assert land.resolve_add_add_conflicts(text) is None


def test_resolve_add_add_conflicts_keeps_identical_ours_and_theirs_once():
    text = "<<<<<<< HEAD\nsame line\n||||||| base\n=======\nsame line\n>>>>>>> theirs\n"
    assert land.resolve_add_add_conflicts(text) == "same line\n"


def test_resolve_add_add_conflicts_refuses_the_whole_file_when_one_hunk_has_a_base():
    text = (
        "<<<<<<< HEAD\nours one\n||||||| base\n=======\ntheirs one\n>>>>>>> theirs\n"
        "middle\n"
        "<<<<<<< HEAD\nours two\n||||||| base\nbase two\n=======\ntheirs two\n>>>>>>> theirs\n"
    )
    assert land.resolve_add_add_conflicts(text) is None
