from datetime import UTC, datetime

from agent_tools.chair_stall import idle_minutes, is_stalled

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def test_idle_minutes_is_the_gap_to_the_last_call_ts():
    assert idle_minutes("2026-09-27T11:29:00Z", "2026-09-27T10:00:00Z", _NOW) == 31.0


def test_is_stalled_is_true_when_both_gaps_clear_the_threshold():
    assert is_stalled(31, 31) is True


def test_is_stalled_is_false_when_both_gaps_fall_short_of_the_threshold():
    assert is_stalled(29, 29) is False


def test_idle_minutes_reads_the_store_s_microsecond_offset_stamps():
    now = datetime(2026, 9, 29, 12, 14, 7, 219637, tzinfo=UTC)
    assert idle_minutes("2026-09-29T11:44:07.219637+00:00", "2026-09-29T11:00:00Z", now) == 30.0
