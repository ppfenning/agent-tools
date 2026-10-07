from pathlib import Path

from agent_tools.forge_github import push_argv


def test_push_argv_with_expected_tip_leases_the_branch():
    assert push_argv(Path("/r"), "b", "abc123") == [
        "git", "-C", "/r", "push", "--force-with-lease=b:abc123", "-u", "origin", "b",
    ]


def test_push_argv_without_expected_tip_is_unchanged():
    assert push_argv(Path("/r"), "b") == ["git", "-C", "/r", "push", "-u", "origin", "b"]
