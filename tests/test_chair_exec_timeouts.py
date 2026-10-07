import subprocess
from pathlib import Path

from agent_tools import chair_exec
from agent_tools.chair_exec import (
    LAND_TIMEOUT_S,
    LOCAL_ARGV_TIMEOUT_S,
    TIMED_OUT,
    Deps,
    _bounded_for_ssh,
    _land,
    _land_phase,
    _recorded,
    local_timeout,
)
from agent_tools.remote_argv import LANE_HOST_TIMEOUT_S

LAND_ACTION = {"kind": "land", "run": "r-1", "task_id": "t1", "repo": "r", "initiative": "alpha"}
LAND_ARGV = ["cox", "runs", "land", "r-1", "--task", "t1", "--repo", "r", "--apply", "--no-claim"]


def _deps(run) -> Deps:
    return Deps(
        run=run,
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "r-1",
        repo_for=lambda action: "r",
    )


def _spy(monkeypatch) -> list[tuple[list[str], float]]:
    seen: list[tuple[list[str], float]] = []

    def fake(argv, cwd=None, timeout=None):
        seen.append((argv, timeout))
        return 0, ""

    monkeypatch.setattr(chair_exec, "run_argv", fake)
    return seen


def test_a_local_argv_is_bounded_by_the_local_default(monkeypatch):
    seen = _spy(monkeypatch)
    _bounded_for_ssh(Path("."))(["cox", "route", "launch", "epic"])
    assert seen == [(["cox", "route", "launch", "epic"], LOCAL_ARGV_TIMEOUT_S)]


def test_a_cox_runs_land_is_bounded_by_the_land_bound():
    assert local_timeout(LAND_ARGV) == LAND_TIMEOUT_S == 2700.0


def test_an_ssh_argv_keeps_the_lane_host_bound(monkeypatch):
    seen = _spy(monkeypatch)
    _bounded_for_ssh(Path("."))(["ssh", "host", "true"])
    assert seen == [(["ssh", "host", "true"], LANE_HOST_TIMEOUT_S)]
    assert LANE_HOST_TIMEOUT_S == 30


def test_a_bare_run_argv_call_is_bounded(monkeypatch):
    seen: list[float] = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(chair_exec.subprocess, "run", fake_run)
    chair_exec.run_argv(["git", "status"])
    assert seen == [LOCAL_ARGV_TIMEOUT_S]


def test_the_land_worker_turns_a_timeout_exception_into_a_failure_naming_the_command():
    def run(argv):
        raise subprocess.TimeoutExpired(argv, LAND_TIMEOUT_S)

    result = _land(LAND_ACTION, _deps(run), {})
    assert result["status"] == "failed"
    assert result["reason"] == f"{' '.join(LAND_ARGV)}: timed out after 45 min"
    assert "cox runs land r-1" in _recorded(result)["reason"]


def test_the_land_worker_turns_the_timed_out_exit_into_a_failure_not_a_needs_chair():
    result = _land(LAND_ACTION, _deps(lambda argv: (TIMED_OUT, "cox: timed out after 2700.0s\n")), {})
    assert result["status"] == "failed"
    assert "needs_chair" not in result
    assert "timed out after 45 min" in result["reason"]


def test_a_phase_land_that_times_out_also_fails_naming_its_command():
    action = {"kind": "land_phase", "run": "r-1", "phase": "p1", "repo": "r"}
    result = _land_phase(action, _deps(lambda argv: (TIMED_OUT, "")), {})
    assert result["status"] == "failed"
    assert "cox runs land r-1 --phase p1" in result["reason"]
