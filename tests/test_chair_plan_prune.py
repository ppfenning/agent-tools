from agent_tools.chair_plan_prune import prune_argv, worktrees_to_prune

MAIN = "worktree /repo\nHEAD aaa\nbranch refs/heads/main\n"
FIX = "worktree /wt/fix\nHEAD bbb\nbranch refs/heads/epic/foo/fix\n"
BUILD = "worktree /wt/build\nHEAD ccc\nbranch refs/heads/epic/foo/build\n"


def test_two_initiative_worktrees_are_both_returned():
    porcelain = "\n".join([MAIN, FIX, BUILD])
    assert worktrees_to_prune(porcelain, "foo") == [
        {"path": "/wt/fix", "branch": "epic/foo/fix"},
        {"path": "/wt/build", "branch": "epic/foo/build"},
    ]


def test_sibling_initiative_sharing_a_prefix_is_not_returned():
    sibling = "worktree /wt/x\nHEAD ddd\nbranch refs/heads/epic/foo-bar/fix\n"
    assert worktrees_to_prune("\n".join([MAIN, sibling, FIX]), "foo") == [
        {"path": "/wt/fix", "branch": "epic/foo/fix"}
    ]


def test_main_checkout_is_not_returned_even_on_an_epic_branch():
    main = "worktree /repo\nHEAD aaa\nbranch refs/heads/epic/foo/fix\n"
    assert worktrees_to_prune("\n".join([main, BUILD]), "foo") == [
        {"path": "/wt/build", "branch": "epic/foo/build"}
    ]


def test_empty_string_returns_empty_list():
    assert worktrees_to_prune("", "foo") == []


def test_other_branch_and_detached_worktrees_are_not_returned():
    other = "worktree /wt/o\nHEAD eee\nbranch refs/heads/feature/foo/fix\n"
    detached = "worktree /wt/d\nHEAD fff\ndetached\n"
    assert worktrees_to_prune("\n".join([MAIN, other, detached]), "foo") == []


def test_argv_orders_every_remove_before_every_branch_delete():
    entries = [
        {"path": "/wt/fix", "branch": "epic/foo/fix"},
        {"path": "/wt/build", "branch": "epic/foo/build"},
    ]
    assert prune_argv(entries) == [
        ["git", "worktree", "remove", "--force", "/wt/fix"],
        ["git", "worktree", "remove", "--force", "/wt/build"],
        ["git", "branch", "-D", "epic/foo/fix"],
        ["git", "branch", "-D", "epic/foo/build"],
    ]


def test_argv_of_no_entries_is_empty():
    assert prune_argv([]) == []
