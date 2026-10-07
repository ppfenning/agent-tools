from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from agent_tools import chair_exec
from agent_tools.chair_revert_ports import CI_WATCH_TIMEOUT_SECONDS, GhForgePort, GitRevertPort, bounded

PR = "https://github.com/o/r/pull/7"


def _runner():
    return bounded(chair_exec.run_argv)


def test_the_bound_is_thirty_minutes() -> None:
    assert CI_WATCH_TIMEOUT_SECONDS == 1800


def test_wait_checks_passes_the_named_bound_to_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        seen.append({"argv": argv, **kwargs})
        return SimpleNamespace(returncode=0, stdout="ci\tpass\t1s\thttp://x\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert GhForgePort(_runner()).wait_checks(PR) == []
    assert seen[0]["argv"] == ["gh", "pr", "checks", PR, "--watch"]
    assert seen[0]["timeout"] == CI_WATCH_TIMEOUT_SECONDS


def test_every_port_call_carries_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[object] = []

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return SimpleNamespace(returncode=0, stdout="abc p1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run = _runner()
    GitRevertPort(run).fetch_main("/r")
    GitRevertPort(run).push("/r", "revert/7")
    GhForgePort(run).merge(PR)
    assert timeouts == [CI_WATCH_TIMEOUT_SECONDS] * 3


def test_a_watch_timeout_is_a_failed_port_naming_the_command_and_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as raised:
        GhForgePort(_runner()).wait_checks(PR)
    reason = str(raised.value)
    assert f"gh pr checks {PR}" in reason
    assert "timed out after 1800s" in reason
