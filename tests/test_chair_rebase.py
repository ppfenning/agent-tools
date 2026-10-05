from agent_tools.chair_rebase import backup_ref, plan_rebase
from agent_tools.chair_types import PhaseBranch


def branch(initiative: str = "init", phase: str = "p1", ahead: int = 2, behind: int = 3) -> PhaseBranch:
    return {
        "initiative": initiative,
        "phase": phase,
        "branch": f"{initiative}/{phase}",
        "ahead": ahead,
        "behind": behind,
        "tip": "abcdef0123456789",
    }


def test_behind_and_ahead_and_relaunching_gives_one_action():
    assert plan_rebase([branch()], {"init"}) == [
        {
            "kind": "rebase_phase",
            "initiative": "init",
            "phase": "p1",
            "branch": "init/p1",
            "tip": "abcdef0123456789",
            "base": "main",
        }
    ]


def test_behind_zero_gives_none():
    assert plan_rebase([branch(behind=0)], {"init"}) == []


def test_ahead_zero_gives_none():
    assert plan_rebase([branch(ahead=0)], {"init"}) == []


def test_not_relaunching_gives_none():
    assert plan_rebase([branch()], {"other"}) == []


def test_two_branches_are_sorted_by_initiative_then_phase():
    got = plan_rebase([branch("b", "p1"), branch("a", "p2"), branch("a", "p1")], {"a", "b"})
    assert [(a["initiative"], a["phase"]) for a in got] == [("a", "p1"), ("a", "p2"), ("b", "p1")]


def test_backup_ref_truncates_the_tip_to_8_characters():
    assert backup_ref("init/p1", "abcdef0123456789") == "backup/init/p1-abcdef01"
