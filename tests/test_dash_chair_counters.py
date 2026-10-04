from datetime import UTC, datetime, timedelta

from agent_tools.dash_chair_counters import chair_counters

# now is 10:00 local at UTC-5, so local midnight is 05:00Z.
NOW = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
OFFSET = timedelta(hours=-5)
RESULTS = [
    {"action": {"kind": "land"}, "status": "landed"},
    {"action": {"kind": "land_phase"}, "status": "landed"},
    {"action": {"kind": "land"}, "status": "refused"},
    {"action": {"kind": "relaunch"}, "status": "done"},
]
ROWS = [
    {"ts": "2026-10-04T04:59:59Z", "kind": "land_phase", "status": "landed"},
    {"ts": "2026-10-04T05:00:00Z", "kind": "land_phase", "status": "landed"},
    {"ts": "2026-10-04T06:00:00Z", "kind": "land", "status": "landed"},
    {"ts": "2026-10-04T07:00:00Z", "kind": "land_phase", "status": "refused"},
    {"ts": "2026-10-04T08:00:00Z", "kind": "land_phase", "status": "landed"},
    {"ts": "not a time", "kind": "land_phase", "status": "landed"},
]
HOUSEKEEPING = "2026-10-04T13:30:00+00:00"


def test_lands_today_is_the_status_line_land_count():
    assert chair_counters(RESULTS, ROWS, HOUSEKEEPING, NOW, OFFSET)["lands_today"] == 2


def test_phases_today_counts_landed_phase_rows_since_local_midnight():
    assert chair_counters(RESULTS, ROWS, HOUSEKEEPING, NOW, OFFSET)["phases_today"] == 2


def test_housekeeping_age_is_seconds_since_the_last_record():
    assert chair_counters(RESULTS, ROWS, HOUSEKEEPING, NOW, OFFSET)["housekeeping_age_s"] == 5400


def test_unparseable_naive_and_future_housekeeping_stamps_are_zero():
    ages = [chair_counters([], [], s, NOW, OFFSET)["housekeeping_age_s"]
            for s in ("garbage", "2026-10-04T13:30:00", "2026-10-04T16:00:00+00:00")]
    assert ages == [0, 0, 0]


def test_all_unknown_input_is_zeros():
    assert chair_counters([], [], None, NOW, OFFSET) == {
        "lands_today": 0, "phases_today": 0, "housekeeping_age_s": 0,
    }
