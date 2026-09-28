"""`squash_phase`: the phase branch folded into its own pr branch as one commit, and the enrichment that
carries the umbrella and each task's own work item onto it, as `tests/test_land_generate.py` does for `cherry_pick`."""

import subprocess as sp

import pytest

from agent_tools import cli


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _own_tempdir(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))


def _repo_with_phase_branch(tmp_path, *, diverge=False):
    """`main` with one commit, an `epic/init/ph` branch one commit ahead of it. `diverge=True` also
    advances `main` past the branch point on the same line the branch touched, so a squash conflicts."""
    root = tmp_path / "repo"
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for args in (["config", "user.email", "t@e"], ["config", "user.name", "t"]):
        _git(root, *args)
    (root / "f.txt").write_text("original\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    _git(root, "checkout", "-qb", "epic/init/ph")
    (root / "f.txt").write_text("epic change\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "phase work")
    _git(root, "checkout", "-q", "main")
    if diverge:
        (root / "f.txt").write_text("main change\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "main moves")
    return root


_STEP = {"kind": "squash_phase", "branch": "epic/init/ph", "onto": "pr/init--ph", "from": "main",
         "subject": "epic init: ph"}


def test_a_clean_squash_leaves_one_commit_on_the_pr_branch_with_the_subject(tmp_path):
    repo = _repo_with_phase_branch(tmp_path)
    ok, detail = cli._execute_land_step(repo, _STEP)
    assert ok, detail
    assert _git(repo, "rev-list", "--count", "main..pr/init--ph") == "1"
    assert _git(repo, "log", "-1", "--format=%s", "pr/init--ph") == "epic init: ph"


def test_a_conflicting_squash_returns_the_file_names_and_leaves_no_pr_branch(tmp_path):
    repo = _repo_with_phase_branch(tmp_path, diverge=True)
    ok, detail = cli._execute_land_step(repo, _STEP)
    assert not ok
    assert detail == "squash of epic/init/ph conflicts in: f.txt"
    assert _git(repo, "branch", "--list", "pr/init--ph") == ""


def test_a_squash_that_adds_nothing_is_refused_and_leaves_no_pr_branch_or_worktree(tmp_path):
    repo = _repo_with_phase_branch(tmp_path)
    _git(repo, "merge", "-q", "--ff-only", "epic/init/ph")
    ok, detail = cli._execute_land_step(repo, _STEP)
    assert (ok, detail) == (False, "epic/init/ph adds nothing over main")
    assert _git(repo, "branch", "--list", "pr/init--ph") == ""
    assert not cli._land_worktree(repo, "pr/init--ph").exists()


def test_the_land_walk_drops_the_squash_worktree_and_keeps_the_pr_branch(tmp_path):
    repo = _repo_with_phase_branch(tmp_path)
    rc, reached, _ = cli._land_execute(repo, [_STEP], [_STEP], None, None, "full", False)
    assert (rc, reached) == (0, ["squash_phase"])
    assert not cli._land_worktree(repo, "pr/init--ph").exists()
    assert _git(repo, "branch", "--list", "pr/init--ph") == "pr/init--ph"


def test_clean_phase_after_squash_phase_leaves_no_pr_branch(tmp_path):
    repo = _repo_with_phase_branch(tmp_path)
    clean = {"kind": "clean_phase", "run": "r", "phase_branch": "epic/init/ph", "tasks": [],
             "worktree_root": str(tmp_path / "wts"), "pr_branch": "pr/init--ph"}
    assert cli._execute_land_step(repo, _STEP)[0]
    assert cli._execute_land_step(repo, clean)[0]
    assert _git(repo, "branch", "--list", "pr/init--ph", "epic/init/ph") == ""


def test_phase_task_items_names_only_approved_items():
    items = [{"id": "a", "status": "approved", "file": "/w/a.md"}, {"id": "d", "status": "done", "file": "/w/d.md"}]
    assert cli._phase_task_items(items) == {"a": "/w/a.md"}


def test_land_enrich_gives_squash_phase_the_umbrella_and_phase_mark_done_its_item():
    steps = [{"kind": "squash_phase", "onto": "pr/init--ph"}, {"kind": "mark_done", "task": "t1"},
             {"kind": "mark_done", "task": "t2"}]
    enriched = cli._land_enrich(steps, path="p", worktree_root="w", umbrella="/u",
                                 task_items={"t1": "work/init/ph/t1.md"})
    assert enriched[0] == {"kind": "squash_phase", "onto": "pr/init--ph", "umbrella": "/u"}
    assert enriched[1] == {"kind": "mark_done", "task": "t1", "path": "p",
                           "item": "work/init/ph/t1.md", "from": "approved", "to": "done"}
    assert enriched[2] == {"kind": "mark_done", "task": "t2", "path": "p"}
