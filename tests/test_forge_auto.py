import subprocess

import pytest

from agent_tools import doctor, forge, forge_auto, forge_forgejo, forge_github, forge_local


@pytest.fixture(autouse=True)
def _no_forgejo_base_url(monkeypatch):
    monkeypatch.delenv("FORGEJO_BASE_URL", raising=False)


def _repo(tmp_path, origin=None):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", origin], check=True)
    return tmp_path


def test_auto_is_a_built_in_forge_with_the_current_protocol():
    assert forge.forge_for("auto") is forge_auto
    assert forge.missing_refs(forge_auto) == []


def test_a_github_origin_lands_through_the_github_forge(tmp_path):
    assert forge_auto.forge_of(_repo(tmp_path, "https://github.com/acme/widgets.git")) is forge_github
    assert forge_auto.forge_of(_repo(tmp_path / "ssh", "git@github.com:acme/widgets.git")) is forge_github


def test_any_other_origin_and_no_origin_land_through_the_local_forge(tmp_path):
    assert forge_auto.forge_of(_repo(tmp_path / "lan", "git@git.example:me/widgets.git")) is forge_local
    assert forge_auto.forge_of(_repo(tmp_path / "bare")) is forge_local


def test_an_origin_on_the_forgejo_base_url_host_lands_through_the_forgejo_forge(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEJO_BASE_URL", "http://git.lan:3000")
    assert forge_auto.forge_of(_repo(tmp_path, "git@git.lan:ppfenning/workspace.git")) is forge_forgejo
    assert forge_auto.forge_of(_repo(tmp_path / "http", "http://git.lan:3000/o/r.git")) is forge_forgejo


def test_a_github_origin_stays_github_with_forgejo_base_url_set(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEJO_BASE_URL", "http://git.lan:3000")
    assert forge_auto.forge_of(_repo(tmp_path, "git@github.com:acme/widgets.git")) is forge_github


def test_the_forgejo_host_without_forgejo_base_url_and_other_hosts_stay_local(tmp_path, monkeypatch):
    assert forge_auto.forge_of(_repo(tmp_path, "git@git.lan:ppfenning/workspace.git")) is forge_local
    monkeypatch.setenv("FORGEJO_BASE_URL", "http://git.lan:3000")
    assert forge_auto.forge_of(_repo(tmp_path / "other", "git@git.example:me/widgets.git")) is forge_local


def test_calls_go_to_the_chosen_forge(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(forge_local, "push", lambda r, b: (True, f"local {b}"))
    monkeypatch.setattr(forge_local, "wait_checks", lambda r, t, *, ref="HEAD": (True, f"local {ref}"))
    assert forge_auto.push(repo, "agents/x") == (True, "local agents/x")
    assert forge_auto.wait_checks(repo, 1.0, ref="abc") == (True, "local abc")
    assert forge_auto.find_open_prs(repo, "agents/x") == []


def test_the_doctor_row_needs_gh_for_the_github_side():
    assert doctor._forge_row({"forge": "auto", "gh_auth": True})["ok"] is True
    row = doctor._forge_row({"forge": "auto", "gh_auth": False})
    assert row["ok"] is False and "github-origin repos cannot land" in row["detail"]
