import shutil
import subprocess

import pytest

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import launch_argv, ssh_argv, sync_argv
from agent_tools.remote_lane import remote_record
from agent_tools.remote_launch import LaunchError, env_preflight, launch_on_host, launch_plan

needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")


def _fake_run(calls, codes=None):
    codes = codes or {}

    def run(argv):
        calls.append(argv)
        if argv[0] == "rsync" and "rsync" not in codes:
            return subprocess.run(argv).returncode
        return codes.get(argv[0], 0)

    return run


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "work" / "init" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "init" / "epic.md").write_text("epic")
    (tmp_path / "work" / "init" / "p1" / "t.md").write_text("task")
    remote = tmp_path / "remote"
    (remote / "work").mkdir(parents=True)
    return LaneHost("box", "me@box", str(remote))


@needs_rsync
def test_a_launch_copies_the_initiative_then_starts_the_lane_and_returns_the_record(tmp_path, monkeypatch):
    host, calls = _setup(tmp_path, monkeypatch), []
    result = launch_on_host(host, "init", "r1", "lbl", "2026-09-25T00:00:00Z", _fake_run(calls), lambda p: p)
    dest = tmp_path / "remote" / "work" / "init"
    assert (dest / "epic.md").read_text() == "epic"
    assert (dest / "p1" / "t.md").read_text() == "task"
    # The remote `route launch` gets the initiative's path in the host's workspace, not a bare id.
    assert calls[-1] == ssh_argv("me@box", launch_argv(f"{tmp_path / 'remote'}/work/init", "r1", "lbl"))
    assert result == remote_record("box", "2026-09-25T00:00:00Z")


def test_a_launch_with_a_repo_returns_a_record_naming_it(tmp_path, monkeypatch):
    host = _setup(tmp_path, monkeypatch)
    result = launch_on_host(host, "init", "r1", "lbl", "t", _fake_run([], {"rsync": 0}), lambda p: p, repo="/r")
    assert result == remote_record("box", "t", "/r")


def test_a_failing_rsync_stops_before_the_ssh_step(tmp_path, monkeypatch):
    host, calls = _setup(tmp_path, monkeypatch), []
    result = launch_on_host(host, "init", "r1", "lbl", "t", _fake_run(calls, {"rsync": 1}), lambda p: p)
    assert isinstance(result, LaunchError) and result.step == "rsync"
    assert [c[0] for c in calls] == ["rsync"]


def test_a_failing_ssh_names_the_ssh_step(tmp_path, monkeypatch):
    host, calls = _setup(tmp_path, monkeypatch), []
    result = launch_on_host(host, "init", "r1", "lbl", "t", _fake_run(calls, {"rsync": 0, "ssh": 255}), lambda p: p)
    assert isinstance(result, LaunchError) and result.step == "ssh"
    assert [c[0] for c in calls] == ["rsync", "ssh"]


def test_a_failing_sync_stops_before_the_launch_argv_runs():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    codes = iter([0, 1])

    def run(argv):
        calls.append(argv)
        return next(codes)

    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", run, repo="/r")
    assert result == LaunchError("sync", "updating /r on box2 exited 1: the lane would build on a stale main")
    assert calls == [
        ["rsync", "-a", "--delete", "work/init-x/", "me@box2:/ws/work/init-x/"],
        ssh_argv("me@box2", sync_argv("/r")),
    ]


def test_the_default_location_is_the_ssh_destination_and_path(tmp_path, monkeypatch):
    host, calls = _setup(tmp_path, monkeypatch), []
    launch_on_host(host, "init", "r1", "lbl", "t", _fake_run(calls, {"rsync": 0}))
    assert calls[0][-1] == f"me@box:{tmp_path}/remote/work/init/"


def test_launch_plan_is_the_rsync_argv_then_the_ssh_argv():
    host = LaneHost("box2", "me@box2", "/ws")
    assert launch_plan(host, "init-x", "init-x-1", "l") == [
        ["rsync", "-a", "--delete", "work/init-x/", "me@box2:/ws/work/init-x/"],
        [
            "ssh",
            "me@box2",
            "cox route launch epic --initiative /ws/work/init-x --run-id init-x-1 --label l --no-claim",
        ],
    ]


def test_launch_plan_without_a_repo_returns_todays_two_argv():
    host = LaneHost("box2", "me@box2", "/ws")
    assert launch_plan(host, "init-x", "init-x-1", "l", repo=None) == launch_plan(host, "init-x", "init-x-1", "l")
    assert len(launch_plan(host, "init-x", "init-x-1", "l", repo=None)) == 2


def test_launch_plan_with_a_repo_returns_three_argv():
    host = LaneHost("box2", "me@box2", "/ws")
    assert launch_plan(host, "init-x", "init-x-1", "l", repo="/r") == [
        ["rsync", "-a", "--delete", "work/init-x/", "me@box2:/ws/work/init-x/"],
        ssh_argv("me@box2", sync_argv("/r")),
        [
            "ssh",
            "me@box2",
            "cox route launch epic --initiative /ws/work/init-x --run-id init-x-1 --label l --no-claim",
        ],
    ]


def test_launch_plan_with_a_harness_dir_syncs_it_before_the_repo():
    host = LaneHost("box2", "me@box2", "/ws")
    plan = launch_plan(host, "init-x", "init-x-1", "l", repo="/r", harness_dir="/h")
    assert plan[1:3] == [ssh_argv("me@box2", sync_argv("/h")), ssh_argv("me@box2", sync_argv("/r"))]
    assert len(plan) == 4


def test_launch_plan_without_a_harness_dir_is_todays_list():
    host = LaneHost("box2", "me@box2", "/ws")
    assert launch_plan(host, "init-x", "init-x-1", "l", repo="/r", harness_dir=None) == launch_plan(
        host, "init-x", "init-x-1", "l", repo="/r"
    )
    assert len(launch_plan(host, "init-x", "init-x-1", "l", harness_dir=None)) == 2


def test_a_failing_harness_sync_stops_before_the_repo_sync_and_the_launch():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    codes = iter([0, 1])

    def run(argv):
        calls.append(argv)
        return next(codes)

    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", run, repo="/r", harness_dir="/h")
    assert result == LaunchError(
        "harness",
        "updating the harness at /h on box2 exited 1: the lane would run an older harness than the store",
    )
    assert calls == [
        ["rsync", "-a", "--delete", "work/init-x/", "me@box2:/ws/work/init-x/"],
        ssh_argv("me@box2", sync_argv("/h")),
    ]


def test_a_passing_harness_sync_reaches_the_launch_argv():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", lambda argv: calls.append(argv) or 0, harness_dir="/h")
    assert calls == launch_plan(host, "init-x", "init-x-1", "l", harness_dir="/h")
    assert calls[1] == ssh_argv("me@box2", sync_argv("/h")) and "route launch epic" in calls[-1][2]
    assert result == remote_record("box2", "t")


def test_launch_on_host_runs_the_planned_argvs_in_order():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", lambda argv: calls.append(argv) or 0)
    assert calls == launch_plan(host, "init-x", "init-x-1", "l")
    assert result == remote_record("box2", "t")


def test_a_preflight_refusal_returns_the_auth_error_before_anything_runs():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    line = "claude auth: not logged in on the host (run claude auth login there)"
    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", lambda argv: calls.append(argv) or 0, preflight=line)
    assert result == LaunchError("auth", line)
    assert calls == []


def test_env_preflight_names_the_first_empty_variable_and_is_none_when_all_are_set():
    assert env_preflight(("MY_API_KEY",), {"MY_API_KEY": (1, "")}) == "env vars: MY_API_KEY is not set on the host (set it there)"
    assert env_preflight(("MY_API_KEY",), {"MY_API_KEY": (0, "sk-1\n")}) is None


def test_env_preflight_reports_a_failed_ssh_and_an_empty_name_list_without_calling_them_unset():
    assert env_preflight(("K",), {"K": (255, "ssh: Connection refused\n")}) == "env var check failed on the host: ssh: Connection refused"
    assert env_preflight((), {}) == "no auth_env or endpoint_env configured: nothing to check on the host"
