"""The gather steps `land.py` plans for an empty phase branch: `cherry_pick` and `patch_apply` built on one pr branch."""

import subprocess as sp

import pytest

from agent_tools import cli


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _own_tempdir(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))


def _commit(repo, name, text, message):
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _repo_with_task_branches(tmp_path, *, conflict=False):
    """`main` holds a.txt and b.txt; `agents/r/one` and `agents/r/two` each add one commit. With
    `conflict`, `agents/r/two` edits a.txt, which `agents/r/one` edits too."""
    repo = tmp_path / "repo"
    sp.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for args in (["config", "user.email", "t@e"], ["config", "user.name", "t"]):
        _git(repo, *args)
    _commit(repo, "a.txt", "a\n", "init")
    _commit(repo, "b.txt", "b\n", "add b")
    _git(repo, "checkout", "-qb", "agents/r/one")
    _commit(repo, "a.txt", "one\n", "task one")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-qb", "agents/r/two")
    _commit(repo, "a.txt" if conflict else "b.txt", "two\n", "task two")
    _git(repo, "checkout", "-q", "main")
    return repo


def _pick(task, **extra):
    return {"kind": "cherry_pick", "branch": f"agents/r/{task}", "commit_subject": f"task {task}", "onto": "pr/i--p",
            "from": "main", "repo": "repo", "task": task, "files_touched": [], **extra}


def test_two_cherry_pick_steps_land_two_commits_in_order(tmp_path):
    repo = _repo_with_task_branches(tmp_path)
    first = cli._execute_land_step(repo, _pick("one"))
    second = cli._execute_land_step(repo, _pick("two"))
    assert first[0] and second[0], (first, second)
    assert _git(repo, "log", "--reverse", "--format=%s", "main..pr/i--p").splitlines() == ["task one", "task two"]
    cli._remove_land_worktree(repo, "pr/i--p")


def test_a_patch_apply_step_produces_the_patchs_change(tmp_path):
    repo = _repo_with_task_branches(tmp_path)
    patch = _git(repo, "diff", "main", "agents/r/one") + "\n"
    step = {"kind": "patch_apply", "branch": "agents/r/one", "patch": patch, "onto": "pr/i--p", "from": "main",
            "repo": "repo", "task": "one", "files_touched": ["a.txt"]}
    ok, detail = cli._execute_land_step(repo, step)
    assert ok, detail
    assert _git(repo, "show", "pr/i--p:a.txt") == "one"
    assert _git(repo, "log", "-1", "--format=%s", "pr/i--p") == "one"
    cli._remove_land_worktree(repo, "pr/i--p")


def test_a_conflicting_cherry_pick_names_the_file_and_leaves_none_in_progress(tmp_path):
    repo = _repo_with_task_branches(tmp_path, conflict=True)
    assert cli._execute_land_step(repo, _pick("one"))[0]
    ok, detail = cli._execute_land_step(repo, _pick("two"))
    wt = cli._land_worktree(repo, "pr/i--p")
    assert not ok
    assert detail == "cherry-pick of agents/r/two conflicts in: a.txt"
    assert sp.run(["git", "-C", str(wt), "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"]).returncode != 0
    assert _git(wt, "status", "--porcelain") == ""
    cli._remove_land_worktree(repo, "pr/i--p")


def test_a_conflict_stops_the_land_before_any_later_step(tmp_path, capsys):
    repo = _repo_with_task_branches(tmp_path, conflict=True)
    steps = [_pick("one"), _pick("two"), {"kind": "pick_branch", "branch": "x", "commit_subject": "later"}]
    rc, reached, _ = cli._land_execute(repo, steps, [], None, None, "pr", False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "cherry_pick: cherry-pick of agents/r/two conflicts in: a.txt" in out
    assert "stopped; remaining: pick_branch" in out
    assert reached == ["cherry_pick", "cherry_pick"]
