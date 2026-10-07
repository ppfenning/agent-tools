"""A no-PR leftover pr branch is recreated, whatever copies of it exist; only an open PR refuses."""

from agent_tools import land

BRANCH = "pr/p--ph1"
LOCAL_REF = "refs/backup/pr/p--ph1"
REMOTE_REF = "refs/backup/origin/pr/p--ph1"


def test_remote_only_recreates_and_backs_up_the_remote_tip():
    assert land.leftover_branch_decision(BRANCH, True, False, None, "t2", local_commit=None,
                                         remote_commit="r1", remote_tree="t1") == {
        "kind": "back_up_and_rebuild", "backup_ref": None, "old_commit": None,
        "local_tip": None, "remote_tip": "r1", "backup_refs": {REMOTE_REF: "r1"}}


def test_local_and_remote_with_no_pr_recreates_and_backs_up_both_tips():
    assert land.leftover_branch_decision(BRANCH, True, False, "t1", "t2", local_commit="c1",
                                         remote_commit="r1", remote_tree="t1") == {
        "kind": "back_up_and_rebuild", "backup_ref": LOCAL_REF, "old_commit": "c1",
        "local_tip": "c1", "remote_tip": "r1", "backup_refs": {LOCAL_REF: "c1", REMOTE_REF: "r1"}}


def test_main_moved_recreates_when_the_pushed_tree_differs_from_the_expected_tree():
    assert land.leftover_branch_decision(BRANCH, True, False, "t2", "t2", local_commit="c1",
                                         remote_commit="r1", remote_tree="t1") == {
        "kind": "back_up_and_rebuild", "backup_ref": LOCAL_REF, "old_commit": "c1",
        "local_tip": "c1", "remote_tip": "r1", "backup_refs": {LOCAL_REF: "c1", REMOTE_REF: "r1"}}


def test_open_pr_still_refuses():
    assert land.leftover_branch_decision(BRANCH, True, True, "t1", "t2", local_commit="c1",
                                         remote_commit="r1", remote_tree="t1") == {
        "kind": "refuse",
        "reason": "local tree t1 differs from the cherry-picked tree t2; remote tree t1 differs from the cherry-picked tree t2"}


def test_phase_resume_recreates_a_local_and_remote_branch_with_no_pr():
    facts = {"local_tip": "a", "remote_tip": "a", "pr": None, "pr_head": None}
    assert land.phase_resume(facts, BRANCH) == {"kind": "recreate"}
