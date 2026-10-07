"""A phase land gathers each approved task the phase branch lacks, and refuses when one has no work to gather."""

import subprocess as sp

import pytest

from agent_tools import cli, land


def _git(repo, *args):
    return sp.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _own_tempdir(tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "tmp"))


def _commit(repo, name, message):
    (repo / name).write_text(message + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _repo(tmp_path, *, second_branch=True):
    """`main`; `agents/r/one` merged into `epic/i/p`; `agents/r/two` ahead of main and absent from `epic/i/p`."""
    repo = tmp_path / "repo"
    sp.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "a.txt", "init")
    _git(repo, "checkout", "-qb", "agents/r/one")
    _commit(repo, "one.txt", "task one")
    _git(repo, "checkout", "-q", "main")
    if second_branch:
        _git(repo, "checkout", "-qb", "agents/r/two")
        _commit(repo, "two.txt", "task two")
        _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-qb", "epic/i/p", "agents/r/one")
    _git(repo, "checkout", "-q", "main")
    return repo


def _record(task, **build):
    return {"run": "r", "task": task, "phase": "p", "initiative": "i", "status": "approved",
            "review": {"verdict": "approve"}, "build": build}


ITEMS = [{"id": "one", "status": "approved"}, {"id": "two", "status": "approved"}]
PHASE = {"run": "r", "phase": "p", "initiative": "i"}


def _plan(repo, records):
    facts = cli._phase_task_facts(repo, "epic/i/p", ITEMS, records)
    return land.land_plan(PHASE, {}, "main", items=ITEMS, task_records=records, task_facts=facts)


@pytest.mark.parametrize("fact, status", [
    ({"reachable": True, "branch_exists": True, "has_patch": True}, "already_present"),
    ({"reachable": False, "branch_exists": True, "has_patch": True}, "gather_from_branch"),
    ({"reachable": False, "branch_exists": False, "has_patch": True}, "gather_from_patch"),
    ({"reachable": False, "branch_exists": False, "has_patch": False}, "missing"),
])
def test_task_gather_status_for_each_status(fact, status):
    assert land.task_gather_status([{"task": "t", **fact}]) == {"t": status}


def test_a_phase_branch_holding_one_task_gathers_the_other(tmp_path):
    repo = _repo(tmp_path)
    steps = _plan(repo, [_record("one"), _record("two")])
    kinds = [s["kind"] for s in steps]
    assert kinds[:3] == ["pick_branch", "squash_phase", "cherry_pick"], kinds
    assert land.phase_landed_tasks(steps) == {"one", "two"}
    for step in steps[1:3]:
        ok, detail = cli._execute_land_step(repo, step)
        assert ok, detail
    assert _git(repo, "log", "--format=%s", "main..pr/i--p").splitlines() == ["task two", "epic i: p"]
    assert _git(repo, "ls-tree", "--name-only", "pr/i--p").split() == ["a.txt", "one.txt", "two.txt"]
    cli._remove_land_worktree(repo, "pr/i--p")


def test_a_task_with_no_branch_and_no_patch_refuses_and_merges_nothing(tmp_path):
    repo = _repo(tmp_path, second_branch=False)
    steps = _plan(repo, [_record("one"), _record("two")])
    assert [s["kind"] for s in steps] == ["refuse"]
    assert steps[0]["reason"].startswith("two: no agent branch")
    assert _git(repo, "branch", "--list", "pr/*") == ""


def test_a_task_with_only_a_patch_is_gathered_from_it(tmp_path):
    repo = _repo(tmp_path, second_branch=False)
    patch = "diff --git a/two.txt b/two.txt\nnew file mode 100644\n--- /dev/null\n+++ b/two.txt\n@@ -0,0 +1 @@\n+two\n"
    steps = _plan(repo, [_record("one"), _record("two", patch=patch)])
    assert [s["kind"] for s in steps][:3] == ["pick_branch", "squash_phase", "patch_apply"]
    assert land.phase_landed_tasks(steps) == {"one", "two"}


def test_phase_task_facts_reads_reachability_branch_and_patch(tmp_path):
    repo = _repo(tmp_path)
    facts = cli._phase_task_facts(repo, "epic/i/p", ITEMS, [_record("one"), _record("two", patch=" ")])
    assert facts == [{"task": "one", "branch_exists": True, "reachable": True, "has_patch": False},
                     {"task": "two", "branch_exists": True, "reachable": False, "has_patch": False}]
    assert cli._phase_task_facts(repo, "epic/i/none", ITEMS, [_record("one")]) is None


def test_a_task_with_only_recorded_files_is_left_to_the_squash_files_check(tmp_path):
    repo = _repo(tmp_path, second_branch=False)
    facts = cli._phase_task_facts(repo, "epic/i/p", ITEMS, [_record("one"), _record("two", files_touched=["two.txt"])])
    assert land.task_gather_status(facts) == {"one": "already_present", "two": "already_present"}
