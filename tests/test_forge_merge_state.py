from types import SimpleNamespace

import pytest

from agent_tools import forge, forge_auto, forge_github, forge_local

cwds: list = []


def _gh(monkeypatch, code=0, out="", err=""):
    calls = []
    cwds.clear()

    def run(argv, **kwargs):
        calls.append(argv)
        cwds.append(kwargs.get("cwd"))
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


def test_merge_state_and_update_branch_run_gh_in_the_repo(monkeypatch, tmp_path):
    # gh finds the PR's repository from the working directory's remote; the chair runs from the workspace,
    # whose remote is not GitHub, so both calls must run in the land's repo (2026-10-06, the-store-holds p2-launch-wire).
    _gh(monkeypatch, out='{"mergeStateStatus":"BEHIND"}')
    forge_github.merge_state(7, repo=tmp_path)
    forge_github.update_branch(7, repo=tmp_path)
    assert cwds == [tmp_path, tmp_path]


def test_auto_forge_delegates_to_the_repos_forge_when_given_a_repo(monkeypatch, tmp_path):
    seen = []
    fake = SimpleNamespace(merge_state=lambda pr, repo: seen.append(("state", pr, repo)) or "CLEAN",
                           update_branch=lambda pr, repo: seen.append(("update", pr, repo)))
    monkeypatch.setattr(forge_auto, "forge_of", lambda repo: fake)
    assert forge_auto.merge_state(7, repo=tmp_path) == "CLEAN"
    forge_auto.update_branch(7, repo=tmp_path)
    assert seen == [("state", 7, tmp_path), ("update", 7, tmp_path)]
