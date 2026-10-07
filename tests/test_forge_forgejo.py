from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_tools import forge_forgejo as ff
from agent_tools import land
from agent_tools.forge import ForgeError
from agent_tools.forgejo_api import read_settings
from tests.fake_forgejo import FakeForgejo

ENV = {"FORGEJO_TOKEN": "tok"}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", "tok")
    with FakeForgejo() as f:
        yield f


@pytest.fixture
def target(fake):
    return ff.Target(read_settings({"forgejo_base_url": fake.base_url, "forgejo_token_env": "FORGEJO_TOKEN"}), "o", "r")


@pytest.fixture
def repo(fake, tmp_path) -> Path:
    def git(*argv):
        return subprocess.run(["git", "-C", str(tmp_path), *argv], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    git("remote", "add", "origin", f"{fake.base_url}/o/r.git")
    return tmp_path


def head_sha(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_open_pr_posts_to_pulls_and_returns_the_address(fake, repo):
    ok, url = ff.open_pr(repo, "Title", "Body", head="feat", base="main")
    assert (ok, url) == (True, f"{fake.base_url}/o/r/pulls/1")
    request = fake.requests[-1]
    assert (request["method"], request["path"]) == ("POST", "/api/v1/repos/o/r/pulls")
    assert request["authorization"] == "token tok"
    assert request["body"] == {"title": "Title", "body": "Body", "head": "feat", "base": "main"}


def test_open_pr_without_a_token_is_a_refusal_not_a_raise(fake, repo, monkeypatch):
    monkeypatch.delenv("FORGEJO_TOKEN")
    ok, detail = ff.open_pr(repo, "T", "B", head="feat", base="main")
    assert not ok and "FORGEJO_TOKEN" in detail


def test_find_open_prs_keeps_the_pulls_with_that_head(fake, repo):
    fake.add_pull(1, "feat", "main", "a" * 40)
    fake.add_pull(2, "other", "main", "b" * 40)
    assert ff.find_open_prs(repo, "feat") == [1]


def test_checks_pending(fake, target):
    fake.set_status("a" * 40, "pending")
    assert ff.read_checks(target, ENV, "a" * 40) == (land.PENDING_RC, "checks pending: status")


def test_checks_pass(fake, target):
    fake.set_status("a" * 40, "success")
    assert ff.read_checks(target, ENV, "a" * 40) == (0, "")


def test_checks_fail(fake, target):
    fake.set_status("a" * 40, "failure")
    assert ff.read_checks(target, ENV, "a" * 40) == (1, "failing checks: status")


def test_no_statuses_is_the_state_forge_github_gives_for_no_checks(fake, target):
    assert ff.read_checks(target, ENV, "c" * 40) == (1, land._NO_CHECKS)


def test_wait_checks_is_green_when_the_head_commit_passes(fake, repo):
    fake.set_status(head_sha(repo), "success")
    assert ff.wait_checks(repo, 5, sleep=lambda s: None) == (True, "green")


def test_wait_checks_reports_the_failing_check(fake, repo):
    fake.set_status(head_sha(repo), "failure")
    assert ff.wait_checks(repo, 5, sleep=lambda s: None) == (False, "failing checks: status")


def test_mergeable_true_and_false(fake, repo):
    fake.add_pull(1, "feat", "main", "a" * 40, mergeable=True)
    assert ff.merge_state(1, repo=repo) == "CLEAN"
    fake.set_mergeable(1, False)
    assert ff.merge_state(1, repo=repo) == "DIRTY"


def test_squash_merge_carries_do_squash(fake, target):
    fake.add_pull(1, "feat", "main", "a" * 40)
    assert ff.squash_merge(target, ENV, 1) == (True, "merged")
    request = fake.requests[-1]
    assert (request["method"], request["path"], request["body"]) == ("POST", "/api/v1/repos/o/r/pulls/1/merge", {"Do": "squash"})


@pytest.mark.parametrize("status", [405, 409])
def test_refused_merge_is_a_value(fake, target, status):
    fake.add_pull(1, "feat", "main", "a" * 40)
    fake.refuse_merge(1, status)
    assert ff.squash_merge(target, ENV, 1) == (False, "not mergeable")


def test_delete_branch(fake, target):
    assert ff.delete_branch(target, ENV, "task/x") == (True, "deleted")
    assert fake.deleted_branches == ["task/x"]


def test_merge_squashes_then_deletes_the_branch(fake, repo):
    fake.add_pull(1, "feat", "main", "a" * 40)
    ok, detail = ff.merge(repo, {"branch": "feat", "default_branch": "main"})
    assert ok and detail.startswith("merged\ndeleted")
    assert fake.deleted_branches == ["feat"]
    assert [r["method"] for r in fake.requests[:3]] == ["GET", "POST", "DELETE"]  # git's own probe of the fake follows


def test_merge_from_the_merged_branch_ends_on_the_default_branch(fake, repo):
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat"], check=True)
    fake.add_pull(1, "feat", "main", "a" * 40)
    ok, detail = ff.merge(repo, {"branch": "feat", "default_branch": "main"})
    assert ok and "deleted local feat" in detail
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout.strip()  # noqa: E731
    assert git("symbolic-ref", "--short", "HEAD") == "main"
    assert git("branch", "--list", "feat") == ""


def test_update_branch_posts_update_and_raises_forge_error_on_refusal(fake, repo):
    with pytest.raises(ForgeError, match="could not update pull request 1: not found"):
        ff.update_branch(1, repo=repo)
    assert (fake.requests[-1]["method"], fake.requests[-1]["path"]) == ("POST", "/api/v1/repos/o/r/pulls/1/update")


def test_wait_checks_polls_pending_until_success(fake, repo):
    sha = head_sha(repo)
    fake.set_status(sha, "pending")
    assert ff.wait_checks(repo, 5, sleep=lambda s: fake.set_status(sha, "success")) == (True, "green")


def test_wait_checks_fails_after_the_unreadable_poll_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEJO_TOKEN", "tok")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "http://127.0.0.1:1/o/r.git"], check=True)
    ok, detail = ff.wait_checks(tmp_path, 5, sleep=lambda s: None)
    assert not ok and detail.startswith(f"checks unreadable {land.POLL_ERROR_LIMIT} polls in a row")


def test_merge_refused_leaves_the_branch(fake, repo):
    fake.add_pull(1, "feat", "main", "a" * 40)
    fake.refuse_merge(1, 409, "conflict")
    assert ff.merge(repo, {"branch": "feat", "default_branch": "main"}) == (False, "conflict")
    assert fake.deleted_branches == []


def test_merge_without_an_open_pr_is_a_refusal(fake, repo):
    assert ff.merge(repo, {"branch": "feat", "default_branch": "main"}) == (False, "no open pull request for feat")


def test_parse_remote():
    assert ff.parse_remote("http://127.0.0.1:3000/o/r.git") == ("http://127.0.0.1:3000", "o", "r")
    assert ff.parse_remote("https://u:pw@git.lan/sub/o/r") == ("https://git.lan/sub", "o", "r")
    assert ff.parse_remote("git@git.lan:o/r.git") is None
    assert ff.parse_remote("https://git.lan/r") is None


def test_parse_open_prs():
    body = [{"number": 3, "head": {"ref": "feat"}}, {"number": 4, "head": {"ref": "x"}}]
    assert ff.parse_open_prs(200, body, "feat") == [3]
    assert ff.parse_open_prs(500, {"message": "boom"}, "feat") == "could not list open pull requests for feat: boom"


def test_parse_open_pr():
    assert ff.parse_open_pr(201, {"html_url": "http://h/o/r/pulls/7"}) == (True, "http://h/o/r/pulls/7")
    assert ff.parse_open_pr(409, {"message": "exists"}) == (False, "exists")
    assert ff.parse_open_pr(201, {}) == (False, "HTTP 201")


def test_parse_checks():
    row = {"context": "ci", "state": "failure"}
    assert ff.parse_checks({"state": "success", "statuses": [{"state": "success"}]}) == (0, "")
    assert ff.parse_checks({"state": "failure", "statuses": [row]}) == (1, "failing checks: ci")
    assert ff.parse_checks({"state": "error", "statuses": [{"state": "error"}]}) == (1, "failing checks: status")
    assert ff.parse_checks({"state": "pending", "statuses": [{"context": "ci", "state": "pending"}]}) == (
        land.PENDING_RC, "checks pending: ci")
    assert ff.parse_checks({"state": "pending", "statuses": []}) == (1, land._NO_CHECKS)
    assert ff.parse_checks({"state": "weird", "statuses": [row]}) == (1, "unrecognised combined status: weird")
    with pytest.raises(ForgeError):
        ff.parse_checks([])


def test_parse_merge_state():
    assert ff.parse_merge_state({"mergeable": True}) == "CLEAN"
    assert ff.parse_merge_state({"mergeable": False}) == "DIRTY"
    assert ff.parse_merge_state({"mergeable": False, "state": "closed", "merged": True}) == "UNKNOWN"
    with pytest.raises(ForgeError):
        ff.parse_merge_state({})


def test_parse_merge_response():
    assert ff.parse_merge_response(200, None) == (True, "merged")
    assert ff.parse_merge_response(405, {"message": "no"}) == (False, "no")
    assert ff.parse_merge_response(409, None) == (False, "HTTP 409")
    with pytest.raises(ForgeError):
        ff.parse_merge_response(500, {"message": "boom"})


def test_parse_delete_response():
    assert ff.parse_delete_response(204, None) == (True, "deleted")
    assert ff.parse_delete_response(404, {"message": "gone"}) == (False, "branch not deleted: gone")
