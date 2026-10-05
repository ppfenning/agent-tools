from agent_tools.chair_plan_review import plan_review


def _entry(state: str, merged_at: str | None = None) -> dict:
    return {
        "initiative": "i", "phase": "p1", "task_id": "a", "repo": "r", "url": "https://x/pr/1",
        "state": state, "merged_at": merged_at,
    }


def test_a_merged_entry_plans_one_review_landed_with_url_and_merged_at():
    facts = {"review_prs": [_entry("merged", "2026-10-04T01:02:03Z")]}
    assert plan_review(facts) == [
        {
            "kind": "review_landed", "initiative": "i", "phase": "p1", "task_id": "a", "repo": "r",
            "url": "https://x/pr/1", "merged_at": "2026-10-04T01:02:03Z",
        }
    ]


def test_a_closed_entry_plans_needs_chair_review_closed_naming_url_and_task():
    assert plan_review({"review_prs": [_entry("closed")]}) == [
        {"kind": "needs_chair", "initiative": "i", "phase": "p1", "task_id": "a", "url": "https://x/pr/1", "cause": "review_closed"}
    ]


def test_an_open_entry_plans_nothing():
    assert plan_review({"review_prs": [_entry("open")]}) == []


def test_plan_tick_carries_a_merged_entry_as_review_landed_stamped_with_the_epoch():
    from agent_tools.chair_plan import plan_tick

    facts = {
        "lease": {"holder": "a", "host": "h", "epoch": 3, "mine": True, "released": False, "stale": False},
        "limits": {"hard_stop": False, "weekly_fraction": 0.4, "hard_stop_fraction": 0.9, "launch_cap": 2, "go_degraded": False},
        "dispatch": {"max_in_flight": 3, "live_runs": 0, "hosts": []},
        "approved": [],
        "initiatives": [],
        "quarantines": [],
        "intake": [],
        "work_store_ready": True,
        "sources_configured": False,
        "review_prs": [_entry("merged", "2026-10-04T01:02:03Z")],
    }
    landed = [a for a in plan_tick(facts) if a["kind"] == "review_landed"]
    assert [(a["url"], a["epoch"]) for a in landed] == [("https://x/pr/1", 3)]
