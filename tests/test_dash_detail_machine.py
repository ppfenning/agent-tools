from agent_tools.dash_detail_machine import _shape

HOST_ROW = {
    "name": "box-1",
    "state": "active",
    "capacity": 4,
    "beat_at": "2026-09-28T11:55:00+00:00",
    "versions_json": (
        '{"cox": "1.2.3", "login_ok": true, "login_checked_at": "2026-09-28T11:00:00+00:00", '
        '"repos": ["/home/box/harness", "/home/box/cartridges"]}'
    ),
}

RUNS = [
    {
        "run_id": "run-a",
        "launched_at": "2026-09-28T09:00:00+00:00",
        "heartbeat_at": "2026-09-28T09:05:00+00:00",
        "ended_at": "2026-09-28T09:10:00+00:00",
    },
    {
        "run_id": "run-b",
        "launched_at": "2026-09-28T10:00:00+00:00",
        "heartbeat_at": "2026-09-28T11:59:00+00:00",
        "ended_at": None,
    },
]

NOW = "2026-09-28T12:00:00+00:00"


def test_shape_builds_the_exact_detail_dict_with_one_ended_and_one_live_lane():
    assert _shape(HOST_ROW, RUNS, NOW) == {
        "host": "box-1",
        "state": "active",
        "capacity": 4,
        "beat_age_s": 300,
        "login_ok": True,
        "login_checked_at": "2026-09-28T11:00:00+00:00",
        "checkouts": [
            {"repo": "/home/box/harness", "commits_behind_main": None},
            {"repo": "/home/box/cartridges", "commits_behind_main": None},
        ],
        "lanes": [
            {
                "run": "run-a",
                "launched_at": "2026-09-28T09:00:00+00:00",
                "heartbeat_at": "2026-09-28T09:05:00+00:00",
                "ended_at": "2026-09-28T09:10:00+00:00",
            },
            {
                "run": "run-b",
                "launched_at": "2026-09-28T10:00:00+00:00",
                "heartbeat_at": "2026-09-28T11:59:00+00:00",
                "ended_at": None,
            },
        ],
    }


def test_shape_with_no_host_row_defaults_every_host_fact_and_keeps_the_lanes():
    runs = [{"run_id": "run-c", "launched_at": "2026-09-28T09:00:00+00:00", "heartbeat_at": None, "ended_at": None}]
    assert _shape(None, runs, NOW) == {
        "host": None,
        "state": None,
        "capacity": None,
        "beat_age_s": None,
        "login_ok": None,
        "login_checked_at": None,
        "checkouts": [],
        "lanes": [
            {"run": "run-c", "launched_at": "2026-09-28T09:00:00+00:00", "heartbeat_at": None, "ended_at": None},
        ],
    }


def test_shape_with_no_runs_gives_an_empty_lane_history():
    assert _shape(HOST_ROW, [], NOW)["lanes"] == []
