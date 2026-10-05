from agent_tools.decompose_empty import empty_outcome, hold_record, relaunch_allowed


def test_nonzero_tasks_is_not_empty():
    assert empty_outcome(3, ["ignored"], True) == {"empty": False}


def test_zero_tasks_with_stub_asks_to_remove_it():
    assert empty_outcome(0, ["no tasks found"], True) == {
        "empty": True,
        "remove_stub": True,
        "reason": "no tasks found",
    }


def test_zero_tasks_without_stub_does_not():
    assert empty_outcome(0, ["no tasks found"], False)["remove_stub"] is False


def test_empty_lint_lines_give_the_fixed_reason():
    assert empty_outcome(0, [], False)["reason"] == "decompose produced no tasks"


def test_lint_lines_are_joined_by_newline():
    assert empty_outcome(0, ["a", "b"], False)["reason"] == "a\nb"


def test_hold_record_shape():
    assert hold_record("work/x/intake.md", 12.5, "why") == {
        "intake": "work/x/intake.md",
        "intake_mtime": 12.5,
        "reason": "why",
    }


def test_no_hold_allows_relaunch():
    assert relaunch_allowed(100.0, None) is True


def test_equal_mtime_blocks_relaunch():
    hold = hold_record("p", 100.0, "r")
    assert relaunch_allowed(100.0, hold) is False


def test_later_mtime_allows_relaunch():
    hold = hold_record("p", 100.0, "r")
    assert relaunch_allowed(100.5, hold) is True
