import pytest

from agent_tools.chair_remedy import choose_remedy, remedy_command, remedy_for, trim_reason


def test_trim_reason_cuts_to_600():
    assert len(trim_reason("x" * 700)) == 600


def test_trim_reason_none_is_empty():
    assert trim_reason(None) == ""


def test_trim_reason_strips():
    assert trim_reason("  why  \n") == "why"


@pytest.mark.parametrize(
    ("cause", "kind"),
    [
        ("ticket", "re-ground"),
        ("code", "re-ground"),
        ("review", "re-ground"),
        ("stranded", "carry"),
        ("harness", "retry"),
        ("stalled", "retry"),
        ("stale", "drop"),
        ("superseded", "drop"),
        ("mystery", "re-ground"),
        (None, "re-ground"),
    ],
)
def test_choose_remedy(cause, kind):
    assert choose_remedy(cause) == kind


def test_remedy_command_unknown_kind_is_reground():
    assert remedy_command("bogus", "r1", "t1", "/repo") == remedy_command("re-ground", "r1", "t1", "/repo")


def test_remedy_for_carry():
    assert remedy_for("stranded", "run-1", "task-a", "/repo") == {
        "kind": "carry",
        "command": ["cox", "runs", "land", "run-1", "--repo", "/repo", "--task", "task-a", "--apply"],
    }


def test_remedy_for_reground():
    assert remedy_for("ticket", "run-1", "task-a", "/repo") == {
        "kind": "re-ground",
        "command": ["python", "-m", "harness.store_cli", "set-state", "run-1", "task-a", "ready", "--by", "chair"],
    }
