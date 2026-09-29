import os
import subprocess
from pathlib import Path

import pytest

from agent_tools.remote_argv import (
    doctor_argv,
    git_fetch_argv,
    launch_argv,
    rsync_pull_argv,
    rsync_push_argv,
    ssh_argv,
    sync_argv,
)


def test_ssh_argv_joins_the_remote_command_into_one_quoted_word():
    assert ssh_argv("me@box", ["echo", "a b", "it's"]) == [
        "ssh",
        "me@box",
        "echo 'a b' 'it'\"'\"'s'",
    ]


def test_doctor_argv_is_the_cox_setup_doctor_command():
    assert doctor_argv() == ["cox", "setup", "doctor"]


def test_launch_argv_places_each_value_after_its_flag():
    assert launch_argv("init-x", "run-7", "lane-a") == [
        "cox", "route", "launch", "epic",
        "--initiative", "init-x",
        "--run-id", "run-7",
        "--label", "lane-a",
        "--no-claim",
    ]


def test_rsync_push_argv_has_one_trailing_slash_each_and_no_delete():
    assert rsync_push_argv("/tmp/src/", "me@box:/srv/dest") == [
        "rsync", "-a", "/tmp/src/", "me@box:/srv/dest/",
    ]


def test_rsync_push_argv_deletes_at_the_destination_only_when_mirroring():
    assert rsync_push_argv("/tmp/src", "me@box:/srv/dest", mirror=True) == [
        "rsync", "-a", "--delete", "/tmp/src/", "me@box:/srv/dest/",
    ]


def test_rsync_pull_argv_passes_the_locations_through():
    assert rsync_pull_argv("me@box:/srv/run/out.json", "/tmp/here") == [
        "rsync", "-a", "me@box:/srv/run/out.json", "/tmp/here",
    ]


def test_git_fetch_argv_maps_the_run_branches_onto_themselves():
    assert git_fetch_argv("me@box:/srv/repo", "r9") == [
        "git", "fetch", "me@box:/srv/repo",
        "refs/heads/agents/r9/*:refs/heads/agents/r9/*",
        "+refs/heads/epic/r9/*:refs/heads/epic/r9/*",
    ]


def test_git_fetch_argv_also_brings_back_the_initiative_phase_branch():
    assert git_fetch_argv("jarvis:/r", "init-a-2") == [
        "git", "fetch", "jarvis:/r",
        "refs/heads/agents/init-a-2/*:refs/heads/agents/init-a-2/*",
        "+refs/heads/epic/init-a/*:refs/heads/epic/init-a/*",
    ]


def test_sync_argv_is_the_sync_argv_s_text():
    assert sync_argv("/some/repo") == [
        "bash",
        "-c",
        'cd "$1" 2>/dev/null || { echo "sync: cannot enter $1" >&2; exit 1; }\n'
        "top=$(git rev-parse --show-toplevel 2>/dev/null)\n"
        '[ "$top" = "$(pwd -P)" ] || { echo "sync: $1 is not the root of a git checkout" >&2; exit 1; }\n'
        "default=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')\n"
        "default=${default:-main}\n"
        "current=$(git symbolic-ref --short -q HEAD)\n"
        '[ "$current" = "$default" ] || '
        '{ echo "sync: checkout is on ${current:-a detached HEAD}, not $default" >&2; exit 1; }\n'
        '[ -z "$(git status --porcelain --untracked-files=no)" ] || '
        '{ echo "sync: tracked changes in the working tree" >&2; exit 1; }\n'
        'git fetch origin && git merge --ff-only "origin/$default"',
        "sync",
        "/some/repo",
    ]


@pytest.fixture
def behind(tmp_path, monkeypatch):
    """A clone on main, one commit behind origin, plus the seed checkout that pushed ahead."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "t")
        monkeypatch.setenv(f"{key}_EMAIL", "t@t")
    origin, seed, clone = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "clone"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "f").write_text("1")
    _git(seed, "add", "f")
    _git(seed, "commit", "-qm", "one")
    _git(seed, "push", "-q", "origin", "main")
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (seed / "f").write_text("2")
    _git(seed, "commit", "-qam", "two")
    _git(seed, "push", "-q", "origin", "main")
    return clone, seed


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _sync(repo: Path, cwd: Path) -> subprocess.CompletedProcess:
    """Runs the command string ssh would hand the remote login shell."""
    remote = ssh_argv("host", sync_argv(str(repo)))[2]
    return subprocess.run(["sh", "-c", remote], cwd=cwd, capture_output=True, text=True)


def test_sync_fast_forwards_a_clean_checkout_and_ignores_untracked_files(behind):
    clone, seed = behind
    (clone / "scratch").write_text("untracked")
    assert _sync(clone, clone.parent).returncode == 0
    assert _git(clone, "rev-parse", "HEAD") == _git(seed, "rev-parse", "HEAD")


def test_sync_falls_back_to_main_without_origin_head(behind):
    clone, seed = behind
    _git(clone, "remote", "set-head", "origin", "-d")
    assert _sync(clone, clone.parent).returncode == 0
    assert _git(clone, "rev-parse", "HEAD") == _git(seed, "rev-parse", "HEAD")


def test_sync_refuses_a_checkout_off_the_default_branch(behind):
    clone, _ = behind
    _git(clone, "checkout", "-qb", "side")
    result = _sync(clone, clone.parent)
    assert (result.returncode, result.stderr) == (1, "sync: checkout is on side, not main\n")


def test_sync_refuses_tracked_changes(behind):
    clone, _ = behind
    before = _git(clone, "rev-parse", "HEAD")
    (clone / "f").write_text("edited")
    result = _sync(clone, clone.parent)
    assert (result.returncode, result.stderr) == (1, "sync: tracked changes in the working tree\n")
    assert _git(clone, "rev-parse", "HEAD") == before


def test_sync_of_a_missing_path_leaves_the_starting_repository_alone(behind):
    clone, _ = behind
    before = _git(clone, "rev-parse", "HEAD")
    gone = clone.parent / "gone"
    result = _sync(gone, cwd=clone)
    assert (result.returncode, result.stderr) == (1, f"sync: cannot enter {gone}\n")
    assert _git(clone, "rev-parse", "HEAD") == before


def test_sync_of_a_plain_directory_inside_a_checkout_refuses(behind):
    clone, _ = behind
    before = _git(clone, "rev-parse", "HEAD")
    (clone / "sub").mkdir()
    result = _sync(clone / "sub", cwd=clone)
    assert (result.returncode, result.stderr) == (1, f"sync: {clone / 'sub'} is not the root of a git checkout\n")
    assert _git(clone, "rev-parse", "HEAD") == before
