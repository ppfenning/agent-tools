from datetime import UTC, datetime, timedelta, timezone

from agent_tools.dash_spend_series import spend_series

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 4, hour, minute, second, tzinfo=UTC)


def test_two_calls_in_one_slot_sum_with_earlier_spend():
    calls = [(at(1, 5), 0.25), (at(1, 12), 0.5), (at(1, 19, 59), 0.125)]
    assert spend_series(calls, NOW) == [
        ["2026-10-04T01:00:00Z", 0.25],
        ["2026-10-04T01:10:00Z", 0.875],
    ]


def test_calls_in_two_slots_give_running_total():
    calls = [(at(0, 3), 1.0), (at(0, 14, 59), 0.5), (at(3, 0), 2.0)]
    assert spend_series(calls, NOW) == [
        ["2026-10-04T00:00:00Z", 1.0],
        ["2026-10-04T00:10:00Z", 1.5],
        ["2026-10-04T03:00:00Z", 3.5],
    ]


def test_unsorted_calls_are_emitted_in_slot_order():
    calls = [(at(2, 0), 2.0), (at(1, 0), 1.0)]
    assert spend_series(calls, NOW) == [
        ["2026-10-04T01:00:00Z", 1.0],
        ["2026-10-04T02:00:00Z", 3.0],
    ]


def test_call_before_midnight_utc_is_dropped():
    calls = [(at(0, 0) - timedelta(seconds=1), 9.0), (at(0, 1), 0.5)]
    assert spend_series(calls, NOW) == [["2026-10-04T00:00:00Z", 0.5]]


def test_call_exactly_at_midnight_is_kept():
    assert spend_series([(at(0, 0), 0.5)], NOW) == [["2026-10-04T00:00:00Z", 0.5]]


def test_call_after_now_is_dropped():
    calls = [(at(11, 59), 1.0), (at(12, 0, 1), 9.0)]
    assert spend_series(calls, NOW) == [["2026-10-04T11:50:00Z", 1.0]]


def test_no_calls_returns_empty_list():
    assert spend_series([], NOW) == []


def test_offset_timestamp_is_bucketed_in_utc():
    plus_two = timezone(timedelta(hours=2))
    call = datetime(2026, 10, 4, 3, 15, 0, tzinfo=plus_two)
    assert spend_series([(call, 0.5)], NOW) == [["2026-10-04T01:10:00Z", 0.5]]
