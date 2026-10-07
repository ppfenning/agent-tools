"""The land edge backs up, pushes the backups of, and rebuilds a leftover pr branch (local, remote or both) under the
repo lease, and still refuses one a PR or a worktree holds."""

import json
import os
import subprocess as sp

import pytest
from test_land import _PRESYNCED, _record

from agent_tools import cli, land_repo_lease, store_cli

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
_TASK = "agents/epic-x-5/seams-task"
_PR = "pr/seams-task"
_STEP = {"kind": "cherry_pick", "branch": _TASK, "commit_subject": "Add seams module", "onto": _PR, "from": "main"}
_BACKUP = f"refs/backup/{_PR}"


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_ENV).stdout.strip()


def _has_ref(repo, ref):
    return sp.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref], capture_output=True).returncode == 0


@pytest.fixture(autouse=True)
def no_open_prs(monkeypatch):
    monkeypatch.setattr(cli, "_open_prs_for", lambda _repo, _branch, _forge=None: [])


@pytest.fixture
def repo(tmp_path):
    """main and a one-commit task branch, both on a real bare origin, and a local-only leftover `pr/seams-task` at main."""
    origin, root = tmp_path / "origin.git", tmp_path / "repo"
    sp.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    root.mkdir()
    sp.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    # The land edge commits (cherry-pick) without _ENV, so CI runners with no global identity need a local one.
    _git(root, "config", "user.name", "t")
    _git(root, "config", "user.email", "t@e")
    (root / "f").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    _git(root, "checkout", "-qb", _TASK)
    (root / "f").write_text("y")
    _git(root, "commit", "-aqm", "Add seams module")
    _git(root, "checkout", "-q", "main")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "main", _TASK)
    _git(root, "branch", _PR, "main")
    return root


def _refusal(repo):
    local, expected = _git(repo, "rev-parse", "main^{tree}"), _git(repo, "rev-parse", f"{_TASK}^{{tree}}")
    return (f"land: refusing, branch {_PR} already exists in {repo}: "
            f"local tree {local} differs from the cherry-picked tree {expected}; files differing: f\n")


def _land(repo, tmp_path, monkeypatch, lease, prs=()):
    """`cox runs land --apply` with the steps faked and the repo lease answering `lease`; returns rc, steps run, stdout."""
    task_dir = tmp_path / "runs/epic-x-5/tasks/seams"
    task_dir.mkdir(parents=True)
    (task_dir / "seams-task.json").write_text(json.dumps(_record()), encoding="utf-8")
    (tmp_path / "runs/policy.tracker.json").write_text('{"tracker": "github-projects"}', encoding="utf-8")
    ran = []
    monkeypatch.setattr(cli, "_execute_land_step", lambda _repo, step, _forge=None: (ran.append(step["kind"]) or True, "https://x/pull/7"))
    monkeypatch.setattr(cli, "_open_prs_for", lambda _repo, _branch, _forge=None: list(prs))
    monkeypatch.setattr(land_repo_lease, "acquire", lambda *_args: lease)
    monkeypatch.setattr(land_repo_lease, "release", lambda *_args: None)
    rc = cli.main(["runs", "land", "epic-x-5", "--repo", str(repo), "--apply", "--runs-dir", str(tmp_path / "runs"), "--gate", "full"])
    return rc, ran


def test_a_local_only_leftover_is_backed_up_and_rebuilt_and_the_land_continues(repo, tmp_path, monkeypatch, capsys):
    old = _git(repo, "rev-parse", _PR)
    rc, ran = _land(repo, tmp_path, monkeypatch, store_cli.LeaseGranted(1, "me"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"land: backed up {old[:8]} to {_BACKUP}, rebuilding {_PR}\n" in out
    assert ran[:10] == _PRESYNCED
    assert _git(repo, "rev-parse", _BACKUP) == old
    assert not _has_ref(repo, f"refs/heads/{_PR}")


def test_the_rebuilt_branch_carries_the_cherry_picked_tree(repo):
    rebuild = cli._land_leftover(repo, _STEP, cli._land_resume(repo, _STEP, None), None)
    assert cli._land_rebuild(repo, _PR, rebuild) is None
    ok, detail = cli._execute_land_step(repo, _STEP)
    assert ok, detail
    assert _git(repo, "rev-parse", f"{_PR}^{{tree}}") == _git(repo, "rev-parse", f"{_TASK}^{{tree}}")


def test_a_refused_repo_lease_leaves_the_leftover_untouched(repo, tmp_path, monkeypatch, capsys):
    old = _git(repo, "rev-parse", _PR)
    rc, ran = _land(repo, tmp_path, monkeypatch, store_cli.LeaseRefused(1, "another land"))
    assert (rc, ran) == (2, [])
    assert "backed up" not in capsys.readouterr().out
    assert _git(repo, "rev-parse", _PR) == old
    assert not _has_ref(repo, _BACKUP)


def test_no_repo_lease_refuses_the_rebuild_with_the_original_message(repo, tmp_path, monkeypatch, capsys):
    rc, ran = _land(repo, tmp_path, monkeypatch, None)  # neither granted nor refused: the store-outage path
    out = capsys.readouterr().out
    assert (rc, ran) == (2, [])
    assert _refusal(repo) in out and "land: not rebuilt: no repo lease to rebuild under\n" in out
    assert not _has_ref(repo, _BACKUP)


def test_the_backup_ref_resolves_to_the_old_commit_and_overwrites_an_older_one(repo):
    old = _git(repo, "rev-parse", _PR)
    _git(repo, "update-ref", _BACKUP, _git(repo, "rev-parse", _TASK))
    rebuild = cli._land_leftover(repo, _STEP, cli._land_resume(repo, _STEP, None), None)
    assert _git(repo, "rev-parse", _BACKUP) != old  # deciding writes nothing
    assert cli._land_rebuild(repo, _PR, rebuild) is None
    assert _git(repo, "rev-parse", _BACKUP) == old


def test_a_leftover_with_an_open_pull_request_still_refuses_with_the_original_message(repo, tmp_path, monkeypatch, capsys):
    rc, ran = _land(repo, tmp_path, monkeypatch, store_cli.LeaseGranted(1, "me"), prs=[7])
    out = capsys.readouterr().out
    assert (rc, ran) == (2, [])
    assert _refusal(repo) in out and "backed up" not in out
    assert not _has_ref(repo, _BACKUP)


def test_an_unreachable_origin_counts_as_holding_the_branch(repo, tmp_path):
    assert cli._remote_branch_exists(repo, _PR) is False
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    assert cli._remote_branch_exists(repo, _PR) is True


def test_a_leftover_checked_out_in_a_worktree_is_not_rebuilt(repo, tmp_path):
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), _PR)
    decision = cli._land_resume(repo, _STEP, None)
    assert cli._land_leftover(repo, _STEP, decision, None) == decision


def test_a_branch_moved_since_the_decision_is_not_deleted(repo):
    rebuild = cli._land_leftover(repo, _STEP, cli._land_resume(repo, _STEP, None), None)
    _git(repo, "branch", "-f", _PR, _TASK)
    assert cli._land_rebuild(repo, _PR, rebuild).startswith(f"{_PR} moved off ")
    assert _git(repo, "rev-parse", _PR) == _git(repo, "rev-parse", _TASK)


_REMOTE_BACKUP = f"refs/backup/origin/{_PR}"


def _remote_tip(repo, ref):
    return _git(repo, "ls-remote", "origin", ref).split("\t")[0]


def _push_pr(repo, *, keep_local):
    """Publish `pr/seams-task` at main, leaving the local branch only when `keep_local`."""
    _git(repo, "push", "-q", "origin", f"main:refs/heads/{_PR}")
    if not keep_local:
        _git(repo, "branch", "-D", _PR)
    return _git(repo, "rev-parse", "main")


def _reject_backups(repo):
    hook = repo.parent / "origin.git/hooks/pre-receive"
    hook.write_text('#!/bin/sh\nwhile read old new ref; do case "$ref" in refs/backup/*) echo no backups >&2; exit 1;; esac; done\n')
    hook.chmod(0o755)


def test_a_remote_only_leftover_is_backed_up_deleted_and_rebuilt(repo, tmp_path, monkeypatch, capsys):
    old = _push_pr(repo, keep_local=False)
    rc, ran = _land(repo, tmp_path, monkeypatch, store_cli.LeaseGranted(1, "me"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"land: backed up {old[:8]} to {_REMOTE_BACKUP}, rebuilding {_PR}\n" in out
    assert _git(repo, "rev-parse", _REMOTE_BACKUP) == _remote_tip(repo, _REMOTE_BACKUP) == old
    assert _remote_tip(repo, f"refs/heads/{_PR}") == ""
    assert not _has_ref(repo, _BACKUP)


def test_a_remote_only_leftover_is_rebuilt_on_the_cherry_picked_tree(repo):
    _push_pr(repo, keep_local=False)
    rebuild = cli._land_leftover(repo, _STEP, cli._land_resume(repo, _STEP, None), None)
    assert rebuild["kind"] == "fresh" and rebuild["local_tip"] is None
    assert cli._land_rebuild(repo, _PR, rebuild) is None
    ok, detail = cli._execute_land_step(repo, _STEP)
    assert ok, detail
    assert _git(repo, "rev-parse", f"{_PR}^{{tree}}") == _git(repo, "rev-parse", f"{_TASK}^{{tree}}")


def test_a_local_and_remote_leftover_with_no_pr_backs_up_both_tips(repo, tmp_path, monkeypatch, capsys):
    old = _push_pr(repo, keep_local=True)
    rc, ran = _land(repo, tmp_path, monkeypatch, store_cli.LeaseGranted(1, "me"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"land: backed up {old[:8]} to {_BACKUP} and {old[:8]} to {_REMOTE_BACKUP}, rebuilding {_PR}\n" in out
    assert _remote_tip(repo, _BACKUP) == _remote_tip(repo, _REMOTE_BACKUP) == old
    assert _remote_tip(repo, f"refs/heads/{_PR}") == "" and not _has_ref(repo, f"refs/heads/{_PR}")


def test_a_branch_pushed_before_main_moved_is_rebuilt(repo, tmp_path, monkeypatch, capsys):
    _git(repo, "branch", "-D", _PR)
    ok, detail = cli._execute_land_step(repo, _STEP)
    assert ok, detail
    cli._remove_land_worktree(repo, _PR)  # the step leaves the branch checked out in its land worktree
    _git(repo, "push", "-q", "origin", _PR)
    pushed = _git(repo, "rev-parse", _PR)
    (repo / "g").write_text("z")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main moves")
    _git(repo, "push", "-q", "origin", "main")
    rc, ran = _land(repo, tmp_path, monkeypatch, store_cli.LeaseGranted(1, "me"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"{pushed[:8]} to {_BACKUP}" in out and f"{pushed[:8]} to {_REMOTE_BACKUP}" in out
    assert _remote_tip(repo, _REMOTE_BACKUP) == pushed
    assert _remote_tip(repo, f"refs/heads/{_PR}") == ""


def test_a_failed_backup_push_leaves_both_branches_untouched(repo, tmp_path, monkeypatch, capsys):
    old = _push_pr(repo, keep_local=True)
    _reject_backups(repo)
    rc, ran = _land(repo, tmp_path, monkeypatch, store_cli.LeaseGranted(1, "me"))
    out = capsys.readouterr().out
    assert (rc, ran) == (2, [])
    assert f"land: not rebuilt: could not back up to {_BACKUP}: " in out and "backed up" not in out
    assert _remote_tip(repo, f"refs/heads/{_PR}") == old
    assert _git(repo, "rev-parse", _PR) == old


def test_a_rerun_finds_the_backup_already_on_origin_and_carries_on(repo, tmp_path, monkeypatch, capsys):
    old = _push_pr(repo, keep_local=False)
    _git(repo, "push", "-q", "origin", f"{old}:{_REMOTE_BACKUP}")
    _git(repo, "update-ref", _REMOTE_BACKUP, old)
    rc, ran = _land(repo, tmp_path, monkeypatch, store_cli.LeaseGranted(1, "me"))
    assert rc == 0, capsys.readouterr().out
    assert _remote_tip(repo, f"refs/heads/{_PR}") == "" and _remote_tip(repo, _REMOTE_BACKUP) == old


def test_a_remote_only_leftover_with_an_open_pull_request_still_refuses(repo, tmp_path, monkeypatch, capsys):
    old = _push_pr(repo, keep_local=False)
    rc, ran = _land(repo, tmp_path, monkeypatch, store_cli.LeaseGranted(1, "me"), prs=[7])
    out = capsys.readouterr().out
    assert (rc, ran) == (2, [])
    assert f"land: refusing, branch {_PR} already exists in {repo}: remote tree " in out and "backed up" not in out
    assert _remote_tip(repo, f"refs/heads/{_PR}") == old and not _has_ref(repo, _REMOTE_BACKUP)
