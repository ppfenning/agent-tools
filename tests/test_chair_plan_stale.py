from datetime import UTC, datetime

from agent_tools.chair_plan_stale import plan_stale
from agent_tools.chair_types import Facts

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_SINCE = _NOW.isoformat()


def _facts(**overrides) -> Facts:
    base = {
        "stale_days": 7,
        "initiatives": [],
        "stale_candidates": [],
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _row(initiative: str, task_id: str, **overrides) -> dict:
    base = {
        "initiative": initiative,
        "task_id": task_id,
        "state": "ready",
        "last_file_change": "2026-09-01T00:00:00+00:00",
        "last_run": None,
        "last_chair_action": None,
        "quarantine_non_harness_count": 0,
    }
    return {**base, **overrides}


def test_one_initiative_with_one_stale_row_gives_one_action_naming_it_and_the_reason():
    facts = _facts(stale_candidates=[_row("a", "a-1")])
    assert plan_stale(facts, _NOW) == [
        {
            "kind": "stale_to_draft",
            "initiative": "a",
            "stale_tasks": ["a-1"],
            "reason": "no file change, run, or chair action in 26 days",
            "since": _SINCE,
        }
    ]


def test_one_initiative_with_two_stale_rows_gives_one_action_listing_both_task_ids():
    facts = _facts(stale_candidates=[_row("a", "a-1"), _row("a", "a-2")])
    [action] = plan_stale(facts, _NOW)
    assert (action["kind"], action["initiative"], action["stale_tasks"]) == ("stale_to_draft", "a", ["a-1", "a-2"])


def test_an_initiative_with_no_stale_row_gives_no_action():
    facts = _facts(stale_candidates=[_row("a", "a-1", state="done")])
    assert plan_stale(facts, _NOW) == []


def test_two_initiatives_each_with_a_stale_row_give_two_actions():
    facts = _facts(stale_candidates=[_row("a", "a-1"), _row("b", "b-1")])
    actions = plan_stale(facts, _NOW)
    assert [(a["kind"], a["initiative"]) for a in actions] == [("stale_to_draft", "a"), ("stale_to_draft", "b")]
