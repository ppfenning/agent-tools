import json
import shutil
import subprocess

import pytest

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_fetch import (
    FetchError,
    chair_repo_path,
    fetch_plan,
    fetch_run,
    host_repo_path,
    pull_argvs,
    refuse_unended,
    task_repos,
)

needs_tools = pytest.mark.skipif(
    shutil.which("rsync") is None or shutil.which("git") is None, reason="rsync and git are required"
)


def _git(cwd, *args):
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    return subprocess.run(["git", *ident, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _real_run(argv):
    return subprocess.run(argv, capture_output=True).returncode


def _world(tmp_path, recorded="host", branch=True):
    """A host and a chair workspace, each with a repo named `repo`. The task record holds the host or the chair path."""
    host_ws, chair_ws = tmp_path / "hostws", tmp_path / "chairws"
    host_repo, chair_repo = host_ws / "repo", chair_ws / "repo"
    for repo in (host_repo, chair_repo):
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
    (host_repo / "f.txt").write_text("x")
    _git(host_repo, "add", "f.txt")
    _git(host_repo, "commit", "-q", "-m", "one")
    if branch:
        _git(host_repo, "branch", "agents/r1/task")
    task = host_ws / "runs" / "r1" / "tasks" / "p1" / "task.json"
    task.parent.mkdir(parents=True)
    task.write_text(json.dumps({"repo": str(host_repo if recorded == "host" else chair_repo)}))
    (host_ws / "runs" / "r1.log").write_text("log")
    return LaneHost("h", "u@h", str(host_ws)), chair_ws / "runs", chair_repo


def test_refuse_unended_needs_a_released_lease_and_an_end_time():
    assert refuse_unended(True, "2026-09-25T00:00:00Z") is None
    assert refuse_unended(False, "2026-09-25T00:00:00Z") is not None
    assert refuse_unended(True, None) is not None
    assert refuse_unended(False, None) is not None


def test_host_repo_path_swaps_the_workspace_prefix_and_keeps_outside_paths():
    assert host_repo_path("/c/ws", "/h/ws/", "/c/ws/repo") == "/h/ws/repo"
    assert host_repo_path("/c/ws", "/h/ws", "/c/ws") == "/h/ws"
    assert host_repo_path("/c/ws", "/h/ws", "/c/ws-other/repo") == "/c/ws-other/repo"
    assert host_repo_path("/c/ws", "/h/ws", "/elsewhere/repo") == "/elsewhere/repo"


def test_chair_repo_path_swaps_the_host_prefix_and_keeps_chair_and_outside_paths():
    assert chair_repo_path("/c/ws", "/h/ws", "/h/ws/repo") == "/c/ws/repo"
    assert chair_repo_path("/c/ws", "/h/ws", "/c/ws/repo") == "/c/ws/repo"
    assert chair_repo_path("/c/ws", "/h/ws", "/h/ws-other/repo") == "/h/ws-other/repo"


def test_pull_argvs_copy_the_run_directory_and_its_log_into_the_runs_dir():
    assert pull_argvs("u@h:/w/runs/r1", "u@h:/w/runs/r1.log", "/c/runs", "r1") == [
        ["rsync", "-a", "u@h:/w/runs/r1/", "/c/runs/r1/"],
        ["rsync", "-a", "u@h:/w/runs/r1.log", "/c/runs/"],
    ]


def test_fetch_plan_pulls_only_the_log_when_the_run_directory_never_existed():
    plan = fetch_plan("u@h:/w/runs/r1", "u@h:/w/runs/r1.log", "/c/runs", "r1", False, True)
    assert plan.pull_argvs == [["rsync", "-a", "u@h:/w/runs/r1.log", "/c/runs/"]]
    assert plan.do_git_fetch is False
    assert plan.outcome == ("fetched: no tasks ran",)


def test_task_repos_lists_each_repo_once_and_skips_records_without_one(tmp_path):
    for name, text in [("a", '{"repo": "/r/one"}'), ("b", '{"repo": "/r/one"}'), ("c", "{}"), ("d", "not json")]:
        path = tmp_path / "tasks" / "p1" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert task_repos(tmp_path) == ["/r/one"]


@needs_tools
@pytest.mark.parametrize("recorded", ["host", "chair"])
def test_fetch_run_brings_the_run_directory_log_and_branch_to_the_chair(tmp_path, recorded):
    host, runs_dir, chair_repo = _world(tmp_path, recorded)
    result = fetch_run(host, "r1", runs_dir, task_repos, _real_run, lambda p: p,
                       lease_released=True, ended_at="2026-09-25T00:00:00Z")
    assert result == (str(chair_repo),)
    assert (runs_dir / "r1" / "tasks" / "p1" / "task.json").is_file()
    assert (runs_dir / "r1.log").read_text() == "log"
    assert _git(chair_repo, "rev-parse", "--verify", "refs/heads/agents/r1/task").strip()


@needs_tools
def test_fetch_run_is_an_error_when_the_host_repo_has_no_branch_for_the_run(tmp_path):
    host, runs_dir, _ = _world(tmp_path, branch=False)
    result = fetch_run(host, "r1", runs_dir, task_repos, _real_run, lambda p: p, lease_released=True, ended_at="t")
    assert isinstance(result, FetchError) and result.step == "verify"


def test_fetch_run_is_an_error_when_no_record_names_a_repo(tmp_path):
    calls = []
    result = fetch_run(LaneHost("h", "u@h", "/w"), "r1", tmp_path / "runs", task_repos,
                       lambda argv: calls.append(argv) or 0, lease_released=True, ended_at="t")
    assert isinstance(result, FetchError) and result.step == "repos"
    # Two probe calls (run directory, log), then the two pull rsyncs the probe's "exists" answers plan.
    assert [c[0] for c in calls] == ["rsync", "rsync", "rsync", "rsync"]


def test_fetch_run_refuses_an_unended_lane_before_running_anything(tmp_path):
    calls = []
    result = fetch_run(LaneHost("h", "u@h", "/w"), "r1", tmp_path / "runs", task_repos,
                       lambda argv: calls.append(argv) or 0, lease_released=False, ended_at=None)
    assert isinstance(result, FetchError) and result.step == "refuse"
    assert calls == []


def test_fetch_run_stops_at_a_failed_rsync_without_a_git_fetch(tmp_path):
    calls = []
    result = fetch_run(LaneHost("h", "u@h", "/w"), "r1", tmp_path / "runs", lambda _: ["/x"],
                       lambda argv: calls.append(argv) or 23, lease_released=True, ended_at="t")
    assert isinstance(result, FetchError) and result.step == "rsync"
    # Both probes read "missing" (23), so the plan falls back to the full pull, whose first rsync
    # then fails the same way.
    assert [c[0] for c in calls] == ["rsync", "rsync", "rsync"]


def test_fetch_run_maps_repos_inside_the_workspace_and_keeps_those_outside(tmp_path):
    calls = []
    runs_dir = tmp_path / "chairws" / "runs"
    inside, outside = str(tmp_path / "chairws" / "repo"), "/elsewhere/repo"
    result = fetch_run(LaneHost("h", "u@h", "/hostws"), "r1", runs_dir, lambda _: [inside, outside],
                       lambda argv: calls.append(argv) or 0, lease_released=True, ended_at="t")
    assert result == (inside, outside)
    refspec = "refs/heads/agents/r1/*:refs/heads/agents/r1/*"
    verify = ["ls-remote", "--exit-code", ".", "refs/heads/agents/r1/*"]
    # calls[:2] are the two "exists" probes, calls[2:4] the pull rsyncs; git starts at 4.
    assert calls[4:] == [
        ["git", "-C", inside, "fetch", "u@h:/hostws/repo", refspec],
        ["git", "-C", inside, *verify],
        ["git", "-C", outside, "fetch", "u@h:/elsewhere/repo", refspec],
        ["git", "-C", outside, *verify],
    ]
