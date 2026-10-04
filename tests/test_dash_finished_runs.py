from datetime import UTC, datetime

from agent_tools.dash_finished_runs import finished_runs

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _rec(run, status, ended_at, verdict=None, **extra):
    return {"run": run, "machine": "m1", "phase": "p1", "node": "build", "attempt": 2, "turns": 7, "cost": 1.5,
            "verdict": verdict, "status": status, "ended_at": ended_at, **extra}


RECORDS = [
    _rec("r10", "approved", "2026-10-04T10:20:00Z", "approve"),
    _rec("r13", "exited", "2026-10-04T05:59:00Z", "approve"),
    _rec("r03", "approved", "2026-10-04T11:30:00Z"),
    _rec("r11", "landed", "2026-10-04T10:10:00Z", "approve"),
    _rec("r01", "exited", "2026-10-04T11:50:00Z", "revise"),
    _rec("r12", "exited", "2026-10-04T11:55:00Z", "approve"),
    _rec("r05", "running", "2026-10-04T11:10:00Z", "revise"),
    _rec("r02", "landed", "2026-10-04T11:40:00Z", "approve"),
    _rec("r04", "quarantined", "2026-10-04T11:20:00Z", "reject"),
    _rec("r09", "exited", "2026-10-04T10:30:00Z", "revise"),
    _rec("r06", "landed", "2026-10-04T11:00:00Z", "approve"),
    _rec("r08", "landed", "2026-10-04T10:40:00Z", "approve"),
    _rec("r07", "exited", "2026-10-04T10:50:00Z", "revise"),
]


def test_finished_runs_selects_newest_ten_with_status_and_verdict():
    rows = finished_runs(RECORDS, {"r12"}, NOW)

    assert [(r["run"], r["status"]) for r in rows] == [
        ("r01", "exited"),
        ("r02", "landed"),
        ("r03", "approved"),
        ("r04", "quarantined"),
        ("r05", "running"),
        ("r06", "landed"),
        ("r07", "exited"),
        ("r08", "landed"),
        ("r09", "exited"),
        ("r10", "approved"),
    ]
    assert [r["verdict"] for r in rows] == [
        "revise", "approve", "", "reject", "revise", "approve", "revise", "approve", "revise", "approve",
    ]
    assert rows[0] == {"run": "r01", "machine": "m1", "phase": "p1", "node": "build", "attempt": 2, "turns": 7,
                       "cost": 1.5, "verdict": "revise", "status": "exited"}
    assert set(rows[0]) == {"run", "machine", "phase", "node", "attempt", "turns", "cost", "verdict", "status"}
    assert finished_runs(RECORDS, {"r12"}, NOW, window_hours=1, limit=3) == [
        _expected("r01", "exited", "revise"), _expected("r02", "landed", "approve"), _expected("r03", "approved", ""),
    ]


def _expected(run, status, verdict):
    return {"run": run, "machine": "m1", "phase": "p1", "node": "build", "attempt": 2, "turns": 7, "cost": 1.5,
            "verdict": verdict, "status": status}
