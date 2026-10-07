"""Every gh and git call in forge_github carries a bound: reads 30 s, everything else 120 s."""

from __future__ import annotations

import subprocess

import pytest

from agent_tools import forge_github
from agent_tools.forge import ForgeError


def _recording(monkeypatch, stdout="{}"):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("timeout")))
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(forge_github.subprocess, "run", run)
    return calls


def _hanging(monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(forge_github.subprocess, "run", run)


def test_constants():
    assert (forge_github.READ_TIMEOUT_S, forge_github.WRITE_TIMEOUT_S) == (30, 120)


def test_read_call_is_given_30(monkeypatch):
    calls = _recording(monkeypatch, '{"mergeStateStatus": "CLEAN"}')
    assert forge_github.merge_state(7) == "CLEAN"
    assert calls == [(["gh", "pr", "view", "7", "--json", "mergeStateStatus"], 30)]


def test_write_call_is_given_120(monkeypatch):
    calls = _recording(monkeypatch)
    forge_github.update_branch(7)
    assert calls == [(["gh", "pr", "update-branch", "7"], 120)]


def test_push_is_given_120(monkeypatch, tmp_path):
    calls = _recording(monkeypatch)
    assert forge_github.push(tmp_path, "feat") == (True, "feat")
    assert [t for _, t in calls] == [120]


def test_timeout_becomes_forge_error_naming_command_and_bound(monkeypatch):
    _hanging(monkeypatch)
    with pytest.raises(ForgeError, match=r"gh pr view 7 --json mergeStateStatus timed out after 30s"):
        forge_github.merge_state(7)
    with pytest.raises(ForgeError, match=r"gh pr update-branch 7 timed out after 120s"):
        forge_github.update_branch(7)


def test_timeout_on_push_is_a_failed_result(monkeypatch, tmp_path):
    _hanging(monkeypatch)
    ok, detail = forge_github.push(tmp_path, "feat")
    assert not ok
    assert "push" in detail and "timed out after 120s" in detail


def test_timeout_on_read_keeps_the_callers_failure_shape(monkeypatch, tmp_path):
    _hanging(monkeypatch)
    assert forge_github.pr_state("http://x/pr/1") == forge_github.UNKNOWN_PR_STATE
    assert "timed out after 30s" in forge_github.find_open_prs(tmp_path, "feat")
