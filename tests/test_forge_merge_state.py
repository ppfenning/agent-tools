from types import SimpleNamespace

import pytest

from agent_tools import forge, forge_auto, forge_github, forge_local


def _gh(monkeypatch, code=0, out="", err=""):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    monkeypatch.setattr(forge_github.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("state", ["BEHIND", "CLEAN", "DIRTY"])
def test_parse_merge_state_returns_the_status_unchanged(state):
    assert forge_github.parse_merge_state(f'{{"mergeStateStatus":"{state}"}}') == state


def test_parse_merge_state_raises_without_the_field():
    with pytest.raises(forge.ForgeError):
        forge_github.parse_merge_state("{}")


def test_merge_state_runs_gh_pr_view(monkeypatch):
    calls = _gh(monkeypatch, out='{"mergeStateStatus":"BEHIND"}')
    assert forge_github.merge_state(7) == "BEHIND"
    assert calls == [["gh", "pr", "view", "7", "--json", "mergeStateStatus"]]


def test_update_branch_runs_gh_pr_update_branch(monkeypatch):
    calls = _gh(monkeypatch)
    assert forge_github.update_branch(7) is None
    assert calls == [["gh", "pr", "update-branch", "7"]]


def test_update_branch_nonzero_exit_raises_with_stderr(monkeypatch):
    _gh(monkeypatch, code=1, err="merge conflict between base and head")
    with pytest.raises(forge.ForgeError, match="merge conflict between base and head"):
        forge_github.update_branch(7)


def test_forges_without_a_host_raise_not_supported():
    for module in (forge_local, forge_auto):
        with pytest.raises(forge.ForgeNotSupported):
            module.merge_state(7)
        with pytest.raises(forge.ForgeNotSupported):
            module.update_branch(7)
