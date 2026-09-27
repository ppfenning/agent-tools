from agent_tools.chair_types import Action, Facts, is_fenced, stamp


def test_stamp_returns_a_new_action_carrying_the_epoch_and_leaves_the_input_alone():
    action: Action = {"kind": "land", "task_id": "t1"}
    stamped = stamp(action, 3)
    assert stamped == {"kind": "land", "task_id": "t1", "epoch": 3}
    assert stamped is not action
    assert action == {"kind": "land", "task_id": "t1"}


def test_an_action_at_the_current_epoch_is_not_fenced():
    assert is_fenced({"kind": "pull", "epoch": 3}, 3) is False


def test_an_action_at_another_epoch_is_fenced():
    assert is_fenced({"kind": "pull", "epoch": 3}, 4) is True


def test_a_rescue_action_stamped_at_epoch_3_is_fenced_only_at_epoch_4():
    rescue = stamp({"kind": "rescue", "initiative": "i", "task_id": "t3"}, 3)
    assert (is_fenced(rescue, 3), is_fenced(rescue, 4)) == (False, True)


def test_a_mark_lost_action_stamped_at_epoch_3_is_fenced_only_at_epoch_4():
    mark_lost = stamp({"kind": "mark_lost", "initiative": "i", "run": "r1"}, 3)
    assert (is_fenced(mark_lost, 3), is_fenced(mark_lost, 4)) == (False, True)


def test_a_stale_to_draft_action_stamped_at_epoch_3_is_fenced_only_at_epoch_4():
    stale_to_draft = stamp(
        {"kind": "stale_to_draft", "initiative": "i", "stale_tasks": ["t3"], "reason": "no file change in 7 days", "since": "2026-09-20T00:00:00Z"},
        3,
    )
    assert (is_fenced(stale_to_draft, 3), is_fenced(stale_to_draft, 4)) == (False, True)


def test_stamp_applied_to_a_land_phase_action_returns_a_new_dict_carrying_the_epoch():
    action: Action = {"kind": "land_phase", "initiative": "i", "phase": "p1", "repo": "r", "run": "run1"}
    stamped = stamp(action, 5)
    assert stamped == {"kind": "land_phase", "initiative": "i", "phase": "p1", "repo": "r", "run": "run1", "epoch": 5}
    assert stamped is not action
    assert action == {"kind": "land_phase", "initiative": "i", "phase": "p1", "repo": "r", "run": "run1"}


def test_a_land_phase_action_is_fenced_only_when_its_epoch_differs():
    land_phase = stamp({"kind": "land_phase", "initiative": "i", "phase": "p1", "repo": "r", "run": "run1"}, 3)
    assert (is_fenced(land_phase, 3), is_fenced(land_phase, 4)) == (False, True)


def test_a_full_facts_literal_has_the_keys_the_planners_read():
    facts: Facts = {
        "lease": {"holder": "a", "host": "h", "epoch": 3, "mine": True, "released": False, "stale": False},
        "limits": {
            "hard_stop": False,
            "weekly_fraction": 0.4,
            "hard_stop_fraction": 0.9,
            "launch_cap": 2,
            "go_degraded": False,
        },
        "dispatch": {"max_in_flight": 3, "live_runs": 1},
        "approved": [{"id": "t1", "initiative": "i", "repo": "r", "phase_done": False, "needs": [], "run": ""}],
        "initiatives": [{"id": "i", "started": True, "ready_tasks": [{"id": "t2", "needs": ["t1"]}], "landed": {"t0"}}],
        "quarantines": [
            {
                "task_id": "t3",
                "initiative": "i",
                "cause": "harness",
                "harness_failures": 1,
                "has_patch": False,
                "rescue_failed": False,
            }
        ],
        "intake": ["n1", "n2"],
        "work_store_ready": True,
        "sources_configured": False,
    }
    assert sorted(facts) == [
        "approved",
        "dispatch",
        "initiatives",
        "intake",
        "lease",
        "limits",
        "quarantines",
        "sources_configured",
        "work_store_ready",
    ]
