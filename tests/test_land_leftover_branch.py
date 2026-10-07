"""A leftover local pr branch is reused, refused, or backed up and rebuilt, decided from plain data."""

from agent_tools import land


def test_equal_trees_reuse():
    assert land.leftover_branch_decision("pr/p--ph1", False, False, "t1", "t1", local_commit="c1") == {"kind": "reuse"}


def test_differing_trees_with_remote_branch_refuse():
    assert land.leftover_branch_decision("pr/p--ph1", True, False, "t1", "t2", local_commit="c1") == {
        "kind": "refuse", "reason": "local tree t1 differs from the cherry-picked tree t2"}


def test_differing_trees_with_open_pull_request_refuse():
    assert land.leftover_branch_decision("pr/p--ph1", False, True, "t1", "t2", local_commit="c1") == {
        "kind": "refuse", "reason": "local tree t1 differs from the cherry-picked tree t2"}


def test_differing_trees_with_neither_back_up_and_rebuild():
    assert land.leftover_branch_decision("pr/p--ph1", False, False, "t1", "t2", local_commit="c1") == {
        "kind": "back_up_and_rebuild", "backup_ref": "refs/backup/pr/p--ph1", "old_commit": "c1"}
