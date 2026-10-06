import json
import shutil
import subprocess

import pytest

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_fetch import (
    FetchError,
    approved_without_branch,
    chair_repo_path,
    fetch_plan,
    fetch_run,
    host_repo_path,
    probe_argv,
    pull_argvs,
    refuse_unended,
    task_repos,
)

RSYNC_BOUND = ["rsync", "-a", "--timeout=30", "-e", "ssh -o ConnectTimeout=30"]
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
        [*RSYNC_BOUND, "u@h:/w/runs/r1/", "/c/runs/r1/"],
        [*RSYNC_BOUND, "u@h:/w/runs/r1.log", "/c/runs/"],
    ]


def test_fetch_plan_pulls_only_the_log_when_the_run_directory_never_existed():
    plan = fetch_plan("u@h:/w/runs/r1", "u@h:/w/runs/r1.log", "/c/runs", "r1", False, True)
    assert plan.pull_argvs == [[*RSYNC_BOUND, "u@h:/w/runs/r1.log", "/c/runs/"]]
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


def test_probe_argv_is_a_bounded_list_only_rsync():
    assert probe_argv("u@h:/w/runs/r1") == [
        "rsync", "--list-only", "-a", "--timeout=30", "-e", "ssh -o ConnectTimeout=30", "u@h:/w/runs/r1", ".",
    ]


def test_fetch_run_hands_run_cmd_a_bounded_rsync_for_the_run_directory(tmp_path):
    calls = []
    fetch_run(LaneHost("h", "u@h", "/w"), "r1", tmp_path / "runs", task_repos,
              lambda argv: calls.append(argv) or 0, lease_released=True, ended_at="t")
    rsyncs = [c for c in calls if c[0] == "rsync" and "u@h:/w/runs/r1/" in c]
    assert rsyncs and all("--timeout=30" in c for c in rsyncs)
    transport = rsyncs[0][rsyncs[0].index("-e") + 1]
    assert transport == "ssh -o ConnectTimeout=30"
    assert all("--timeout=30" in c and "ConnectTimeout=30" in c[c.index("-e") + 1] for c in calls if c[0] == "rsync")


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
    phase_refspec = "+refs/heads/epic/r1/*:refs/heads/epic/r1/*"
    verify = ["ls-remote", "--exit-code", ".", "refs/heads/agents/r1/*"]
    # calls[:2] are the two "exists" probes, calls[2:4] the pull rsyncs; git starts at 4.
    assert calls[4:] == [
        ["git", "-C", inside, "fetch", "u@h:/hostws/repo", refspec, phase_refspec],
        ["git", "-C", inside, *verify],
        ["git", "-C", outside, "fetch", "u@h:/elsewhere/repo", refspec, phase_refspec],
        ["git", "-C", outside, *verify],
    ]


def _record(task, approved):
    verdict = {"verdict": "approve" if approved else "reject"}
    return {"run": "r1", "task": task, "repo": "/elsewhere/repo", "review": verdict, "adversary": verdict}


def test_approved_without_branch_is_empty_when_every_task_is_quarantined():
    assert approved_without_branch([_record("t1", False), _record("t2", False)], frozenset(), "r1") == []


def test_approved_without_branch_names_the_approved_task_whose_branch_is_absent():
    assert approved_without_branch([_record("t1", False), _record("t2", True)], frozenset(), "r1") == ["t2"]


def test_approved_without_branch_is_empty_when_each_approved_task_has_a_branch():
    branches = frozenset(["agents/r1/t1", "agents/r1/t2"])
    assert approved_without_branch([_record("t1", True), _record("t2", True)], branches, "r1") == []


def _fetch_with(tmp_path, records, branches):
    """`fetch_run` against a fake `run_cmd` that has exactly `branches`; its `ls-remote` finds one only if any exist."""
    task_dir = tmp_path / "runs" / "r1" / "tasks" / "p1"
    task_dir.mkdir(parents=True)
    for i, record in enumerate(records):
        (task_dir / f"{i}.json").write_text(record if isinstance(record, str) else json.dumps(record))

    def run_cmd(argv):
        if "rev-parse" in argv:
            return 0 if argv[-1].removeprefix("refs/heads/") in branches else 1
        if "ls-remote" in argv:
            return 0 if branches else 2
        return 0

    return fetch_run(LaneHost("h", "u@h", "/w"), "r1", tmp_path / "runs", task_repos, run_cmd, lambda p: p,
                     lease_released=True, ended_at="t")


def test_fetch_run_is_done_when_no_task_was_approved_and_no_branch_arrived(tmp_path):
    assert _fetch_with(tmp_path, [_record("t1", False)], set()) == ("/elsewhere/repo",)


def test_fetch_run_fails_verify_naming_an_approved_task_with_no_branch(tmp_path):
    result = _fetch_with(tmp_path, [_record("t1", False), _record("t2", True)], set())
    assert isinstance(result, FetchError) and result.step == "verify" and "t2" in result.message


def test_fetch_run_is_done_when_every_approved_task_has_its_branch(tmp_path):
    branches = {"agents/r1/t1", "agents/r1/t2"}
    assert _fetch_with(tmp_path, [_record("t1", True), _record("t2", True)], branches) == ("/elsewhere/repo",)


def test_fetch_run_lets_the_branch_check_decide_when_a_record_is_unreadable(tmp_path):
    result = _fetch_with(tmp_path, [_record("t1", False), "not json"], set())
    assert isinstance(result, FetchError) and result.step == "verify"
