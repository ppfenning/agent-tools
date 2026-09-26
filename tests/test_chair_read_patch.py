from agent_tools.chair_read_patch import has_patch


def test_a_non_blank_patch_is_kept():
    assert has_patch({"build": {"patch": "diff --git a b"}}) is True


def test_a_whitespace_only_patch_is_not_kept():
    assert has_patch({"build": {"patch": "  \n"}}) is False


def test_a_record_without_build_has_no_patch():
    assert has_patch({"task": "x"}) is False


def test_no_record_has_no_patch():
    assert has_patch(None) is False
