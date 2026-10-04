from datetime import UTC, datetime

from agent_tools import console_screen, dash_feed

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _lane(run: str) -> console_screen.LaneRow:
    return console_screen.LaneRow(run, None, "", "p3", "build", 1, 4, 0.5, 0, 3)


def _record(run: str, hour: int, minute: int, status: str) -> dict:
    return {
        "run": run, "machine": "omarchy", "phase": "p2", "node": "review", "attempt": 1, "turns": 5, "cost": 0.25,
        "verdict": "approve", "status": status, "ended_at": f"2026-10-04T{hour:02d}:{minute:02d}:00Z",
    }


def test_runs_are_the_live_lanes_then_the_newest_ten_finished_runs():
    records = [
        *(_record(f"done-{m:02d}", 11, m, "landed" if m % 2 else "quarantined") for m in range(1, 13)),
        _record("live-1", 11, 59, "exited"),
        _record("old-1", 4, 0, "landed"),
    ]

    runs = dash_feed._runs_v1([_lane("live-1"), _lane("live-2")], "omarchy", records, NOW)

    assert [r["run"] for r in runs] == ["live-1", "live-2", *(f"done-{m:02d}" for m in range(12, 2, -1))]
    assert [r["status"] for r in runs[:2]] == ["running", "running"]
    assert [r["status"] for r in runs[2:6]] == ["quarantined", "landed", "quarantined", "landed"]
    assert len(runs) == 12
    assert all(set(r) == set(runs[0]) for r in runs)
