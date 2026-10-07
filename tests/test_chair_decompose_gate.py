import json

from agent_tools.chair_decompose_streak import DecomposeRun, DecomposeStreak
from agent_tools.chair_facts import decompose_stalled_keys, decompose_streak_facts
from agent_tools.chair_plan import plan_tick
from agent_tools.chair_types import Facts

PATH = "2026-10-06-some-idea.md"
REFUSAL = "run x approved but not executed: no lane"
LIMIT = DecomposeStreak(empty_count=2, run_ids=("some-idea-1", "some-idea-2"), first_refusal=REFUSAL)


def _facts(**overrides) -> Facts:
    base = {
        "lease": {"holder": "a", "host": "h", "epoch": 7, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.1, "hard_stop_fraction": 0.9, "launch_cap": 5, "go_degraded": False},
        "dispatch": {"max_in_flight": 5, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [PATH],
        "work_store_ready": True,
        "sources_configured": True,
        "remote_unfetched": {},
        "run_exited": {},
        "lost_runs": {},
        "last_housekeeping_at": None,
        "housekeeping_hours": 24.0,
        "stale_days": 7,
        "stale_candidates": [],
        "stall_candidates": [],
        "home": {},
    }
    return {**base, **overrides}  # type: ignore[return-value]


def _kinds(actions, kind):
    return [a for a in actions if a["kind"] == kind]


def test_an_intake_at_the_limit_files_one_needs_chair_and_no_launch():
    actions = plan_tick(_facts(decompose_streaks={PATH: LIMIT}))
    assert _kinds(actions, "launch_decompose") == []
    stalled = _kinds(actions, "needs_chair")
    assert len(stalled) == 1
    assert stalled[0]["cause"] == "decompose_stalled"
    assert stalled[0]["initiative"] == "some-idea"
    assert stalled[0]["run"] == "some-idea-2"
    assert PATH in stalled[0]["reason"]
    assert "some-idea-1, some-idea-2" in stalled[0]["reason"]
    assert REFUSAL in stalled[0]["reason"]


def test_a_streak_without_a_refusal_line_says_so():
    streak_ = DecomposeStreak(empty_count=2, run_ids=("some-idea-1", "some-idea-2"), first_refusal=None)
    stalled = _kinds(plan_tick(_facts(decompose_streaks={PATH: streak_})), "needs_chair")
    assert "no refusal line was printed" in stalled[0]["reason"]


def test_an_intake_absent_from_the_streaks_still_launches():
    actions = plan_tick(_facts(decompose_streaks={}))
    assert _kinds(actions, "launch_decompose") == [{"kind": "launch_decompose", "intake_ids": [PATH], "epoch": 7}]
    assert _kinds(actions, "needs_chair") == []


def test_a_reported_stall_adds_no_needs_chair_and_still_no_launch():
    facts = _facts(decompose_streaks={PATH: LIMIT}, decompose_stalled_reported=["some-idea|some-idea-2"])
    actions = plan_tick(facts)
    assert _kinds(actions, "needs_chair") == []
    assert _kinds(actions, "launch_decompose") == []


def test_a_newer_run_than_the_reported_one_is_reported_again():
    facts = _facts(decompose_streaks={PATH: LIMIT}, decompose_stalled_reported=["some-idea|some-idea-1"])
    assert len(_kinds(plan_tick(facts), "needs_chair")) == 1


def test_a_stalled_intake_with_no_ready_work_plans_no_pull():
    facts = _facts(work_store_ready=False, decompose_streaks={PATH: LIMIT})
    assert _kinds(plan_tick(facts), "pull") == []


def test_only_the_stalled_intake_is_skipped():
    other = "2026-10-07-other.md"
    actions = plan_tick(_facts(intake=[PATH, other], decompose_streaks={PATH: LIMIT}))
    assert [a["intake_ids"] for a in _kinds(actions, "launch_decompose")] == [[other]]


def test_a_stalled_needs_chair_row_yields_its_key():
    rows = [
        {"kind": "needs_chair", "ts": "t1", "action_json": json.dumps(
            {"kind": "needs_chair", "initiative": "some-idea", "run": "some-idea-2", "cause": "decompose_stalled"}
        )},
        {"kind": "needs_chair", "ts": "t2", "action_json": json.dumps(
            {"kind": "needs_chair", "initiative": "m", "run": "m-1", "cause": "empty_decompose"}
        )},
        {"kind": "launch_decompose", "ts": "t3", "action_json": json.dumps({"kind": "launch_decompose"})},
    ]
    assert decompose_stalled_keys(rows) == ["some-idea|some-idea-2"]


def test_streak_facts_keep_only_intakes_at_the_limit():
    runs = {
        "a.md": [DecomposeRun("a-1", False, None), DecomposeRun("a-2", False, "r")],
        "b.md": [DecomposeRun("b-1", False, None), DecomposeRun("b-2", True, None)],
        "c.md": [DecomposeRun("c-1", False, None)],
    }
    got = decompose_streak_facts(["a.md", "b.md", "c.md"], lambda p: runs[p])
    assert got == {"a.md": DecomposeStreak(empty_count=2, run_ids=("a-1", "a-2"), first_refusal="r")}
