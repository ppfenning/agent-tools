from agent_tools.chair_stale import stale_reason

NOW = "2026-01-08T00:00:00Z"


def test_blocked_task_with_an_eight_day_old_signal_is_stale_at_seven_days():
    assert (
        stale_reason("blocked", "2025-12-31T00:00:00Z", None, None, 0, 7, NOW)
        == "no file change, run, or chair action in 8 days"
    )


def test_blocked_task_with_a_six_day_old_signal_is_not_stale_at_seven_days():
    assert stale_reason("blocked", "2026-01-02T00:00:00Z", None, None, 0, 7, NOW) is None


def test_most_recent_signal_wins_even_when_the_others_are_older():
    assert (
        stale_reason(
            "blocked",
            "2025-12-01T00:00:00Z",
            "2026-01-06T00:00:00Z",
            "2025-11-01T00:00:00Z",
            0,
            7,
            NOW,
        )
        is None
    )


def test_done_state_never_counts_even_with_every_signal_absent():
    assert stale_reason("done", None, None, None, 0, 7, NOW) is None


def test_quarantined_twice_for_a_non_harness_cause_counts_regardless_of_age():
    assert (
        stale_reason("blocked", "2026-01-07T00:00:00Z", None, None, 2, 7, NOW)
        == "quarantined twice for a non-harness cause"
    )


def test_approved_task_with_no_signal_at_all_says_no_activity_recorded():
    assert stale_reason("approved", None, None, None, 0, 7, NOW) == "no activity recorded"
