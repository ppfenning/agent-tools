from agent_tools.remote_argv import (
    doctor_argv,
    git_fetch_argv,
    launch_argv,
    rsync_pull_argv,
    rsync_push_argv,
    ssh_argv,
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
