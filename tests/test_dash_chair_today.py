from datetime import UTC, datetime, timedelta

from agent_tools.dash_chair_today import chair_today

# now is 10:00 local at UTC-5, so local midnight is 05:00Z; a UTC-midnight cutoff would also count the first row.
NOW = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
OFFSET = timedelta(hours=-5)
ROWS = [
    {"ts": "2026-10-04T04:59:59Z", "kind": "land", "status": "landed"},
    {"ts": "2026-10-04T05:00:00Z", "kind": "land", "status": "landed"},
    {"ts": "2026-10-04T06:00:00Z", "kind": "launch_epic", "status": "done"},
    {"ts": "2026-10-04T07:00:00Z", "kind": "land", "status": "refused"},
    {"ts": "2026-10-04T08:00:00Z", "kind": "relaunch", "status": "failed"},
    {"ts": "2026-10-04T09:00:00Z", "kind": "housekeeping", "status": "done"},
]


def test_counts_only_rows_since_local_midnight():
    assert chair_today(ROWS, 3, NOW, OFFSET) == {
        "lands": 1, "launches": 1, "refused_or_failed": 2, "needs_chair_open": 3,
    }
