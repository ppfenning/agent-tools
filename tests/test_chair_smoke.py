import subprocess
from collections.abc import Callable

import pytest

from agent_tools import chair_smoke
from agent_tools.chair_smoke import (
    clear_hold,
    hold_record,
    read_hold,
    revert_pr_argv,
    run_revert_pr,
    smoke_result,
    smoke_verdict,
    write_hold,
)
from agent_tools.land import pr_footer

_OK = {"command": ["cox", "route", "context"], "ok": True, "tail": "fine"}
_LAND = {"repo": "org/repo", "pr": 42, "commit": "abc123"}


def test_smoke_verdict_all_ok() -> None:
    results = [_OK, dict(_OK), dict(_OK)]
    assert smoke_verdict(results) == (True, None)


def test_smoke_verdict_first_failure() -> None:
    failing = {
        "command": ["cox", "runs", "top", "--once"],
        "ok": False,
        "tail": "Traceback (most recent call last):\nboom",
    }
    results = [_OK, failing, dict(_OK)]
    assert smoke_verdict(results) == (False, failing)


def test_hold_record_caps_tail_and_sets_cause() -> None:
    land = {"repo": "org/repo", "pr": 7, "commit": "deadbeef"}
    failing = {
        "command": ["cox", "chair", "run", "--once", "--dry-run"],
        "ok": False,
        "tail": "\n".join(f"line {i}" for i in range(30)),
    }
    record = hold_record(land, failing)
    assert record["land"] == land
    assert record["land"] is not land
    assert record["failing_command"] == failing["command"]
    assert record["cause"] == "smoke_failed"
    assert record["tail"].splitlines() == [f"line {i}" for i in range(10, 30)]


def test_write_then_read_hold_round_trips(tmp_path) -> None:
    runs_dir = str(tmp_path)
    record = {
        "land": {"repo": "org/repo", "pr": 7, "commit": "deadbeef"},
        "failing_command": ["cox", "route", "context"],
        "tail": "boom",
        "cause": "smoke_failed",
    }
    write_hold(runs_dir, record)
    assert read_hold(runs_dir) == record


def test_clear_hold_then_read_hold_is_none(tmp_path) -> None:
    runs_dir = str(tmp_path)
    write_hold(runs_dir, {"land": {}, "failing_command": [], "tail": "", "cause": "smoke_failed"})
    clear_hold(runs_dir)
    assert read_hold(runs_dir) is None


def test_smoke_result_clean_exit_is_ok() -> None:
    result = smoke_result(["cox", "route", "context"], 0, "all good", timed_out=False)
    assert result == {"command": ["cox", "route", "context"], "ok": True, "tail": "all good", "timed_out": False}


def test_smoke_result_nonzero_exit_is_not_ok() -> None:
    result = smoke_result(["cox", "route", "context"], 1, "boom", timed_out=False)
    assert result["ok"] is False


def test_smoke_result_traceback_in_output_is_not_ok_even_on_exit_0() -> None:
    result = smoke_result(["cox", "route", "context"], 0, "Traceback (most recent call last):\nboom", timed_out=False)
    assert result["ok"] is False


def test_smoke_result_timed_out_is_not_ok_even_on_exit_0_clean_output() -> None:
    result = smoke_result(["cox", "route", "context"], 0, "still running", timed_out=True)
    assert result["ok"] is False
    assert result["timed_out"] is True


def test_smoke_verdict_timeout_is_inconclusive_not_a_failure() -> None:
    timed_out = smoke_result(["cox", "chair", "run", "--once", "--dry-run"], 124, "timed out", timed_out=True)
    assert smoke_verdict([_OK, timed_out]) == (True, None)


def test_smoke_verdict_failure_after_a_timeout_still_fails() -> None:
    timed_out = smoke_result(["cox", "route", "context"], 124, "timed out", timed_out=True)
    failing = smoke_result(["cox", "runs", "top", "--once"], 1, "boom", timed_out=False)
    assert smoke_verdict([timed_out, failing]) == (False, failing)


def test_revert_pr_create_runs_in_the_checkout_without_a_repo_flag() -> None:
    argv = revert_pr_argv({"repo": "/home/me/repos/tools", "pr": 7, "commit": "abc"}, "boom")
    assert "--repo" not in argv["pr_create"]
    assert argv["cwd"] == "/home/me/repos/tools"


def test_revert_pr_argv_carries_commit_and_pr() -> None:
    argv = revert_pr_argv(_LAND, "boom")
    assert "abc123" in argv["revert"]
    assert any("42" in part for part in argv["checkout"])
    assert any("42" in part for part in argv["pr_create"])


def test_revert_pr_body_ends_with_footer() -> None:
    argv = revert_pr_argv(_LAND, "boom", "r-1")["pr_create"]
    body = argv[argv.index("--body") + 1]
    assert body == "boom\n\n" + pr_footer("r-1")
    assert body.endswith(" · run r-1")


def _fake_run(calls: list[list[str]], fail_on: str | None) -> Callable[..., None]:
    def run(argv: list[str], check: bool, cwd: str | None = None) -> None:
        calls.append(argv)
        if fail_on is not None and fail_on in argv:
            raise subprocess.CalledProcessError(1, argv)

    return run


def test_run_revert_pr_runs_four_steps_in_order(monkeypatch) -> None:
    argv = revert_pr_argv(_LAND, "boom")
    calls: list[list[str]] = []
    monkeypatch.setattr(chair_smoke.subprocess, "run", _fake_run(calls, None))
    run_revert_pr(argv)
    assert calls == [argv["checkout"], argv["revert"], argv["push"], argv["pr_create"]]


def test_run_revert_pr_stops_when_checkout_fails(monkeypatch) -> None:
    argv = revert_pr_argv(_LAND, "boom")
    calls: list[list[str]] = []
    monkeypatch.setattr(chair_smoke.subprocess, "run", _fake_run(calls, "checkout"))
    with pytest.raises(subprocess.CalledProcessError):
        run_revert_pr(argv)
    assert calls == [argv["checkout"]]
