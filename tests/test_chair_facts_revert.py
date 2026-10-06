from dataclasses import replace

from agent_tools.chair_facts import gather_facts, landed_main_facts
from agent_tools.chair_types import HoldRecord, LandWatch
from tests.test_chair_facts import NOW, _deps

WATCH: LandWatch = {"initiative": "i", "phase": "p1", "repo": "/r", "pr": 7, "commit": "abc"}


def _hold(commit: str) -> HoldRecord:
    return {
        "land": {"repo": "/r", "pr": 7, "commit": commit},
        "failing_command": ["pytest"],
        "tail": "1 failed",
        "cause": "smoke_failed",
    }


def test_red_main_ci_yields_ci_red_with_its_output():
    [row] = landed_main_facts([WATCH], lambda repo, commit: ("red", "boom"), None)
    assert (row["ci"], row["ci_output"]) == ("red", "boom")


def test_green_main_ci_yields_ci_green():
    [row] = landed_main_facts([WATCH], lambda repo, commit: ("green", ""), None)
    assert row["ci"] == "green"


def test_pending_main_ci_yields_ci_pending():
    [row] = landed_main_facts([WATCH], lambda repo, commit: ("pending", ""), None)
    assert row["ci"] == "pending"


def test_a_hold_for_the_watch_commit_fails_smoke_with_its_tail():
    [row] = landed_main_facts([WATCH], lambda repo, commit: ("green", ""), _hold("abc"))
    assert (row["smoke"], row["smoke_output"]) == ("failed", "1 failed")


def test_a_hold_for_another_commit_does_not_fail_this_watch():
    [row] = landed_main_facts([WATCH], lambda repo, commit: ("green", ""), _hold("zzz"))
    assert row["smoke"] == "ok"


def test_no_watches_yield_an_empty_landed_main():
    assert landed_main_facts([], lambda repo, commit: ("green", ""), None) == []


def test_outcomes_pass_through_unchanged_and_default_to_empty():
    outcomes = {"i": ["reverted", "held"]}
    assert gather_facts(replace(_deps(), land_outcomes=lambda: outcomes), NOW)["land_outcomes"] == outcomes
    facts = gather_facts(_deps(), NOW)
    assert (facts["land_outcomes"], facts["landed_main"]) == ({}, [])
