from agent_tools.chair_read_housekeeping import latest_housekeeping


def test_latest_housekeeping_picks_the_newest_of_three_and_ignores_a_land_row_and_a_dry_run_row():
    rows = [
        {"kind": "housekeeping", "status": "done", "ts": "2026-09-25T00:00:00Z"},
        {"kind": "housekeeping", "status": "done", "ts": "2026-09-27T00:00:00Z"},
        {"kind": "housekeeping", "status": "failed", "ts": "2026-09-26T00:00:00Z"},
        {"kind": "land", "status": "done", "ts": "2026-09-28T00:00:00Z"},
        {"kind": "housekeeping", "status": "dry_run", "ts": "2026-09-29T00:00:00Z"},
    ]
    assert latest_housekeeping(rows) == "2026-09-27T00:00:00Z"


def test_latest_housekeeping_of_no_rows_is_none():
    assert latest_housekeeping([]) is None
